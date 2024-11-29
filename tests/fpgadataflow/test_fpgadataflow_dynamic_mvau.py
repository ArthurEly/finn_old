# Copyright (C) 2024, Advanced Micro Devices, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# * Neither the name of FINN nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import pytest

import numpy as np
import qonnx.custom_op.general.xnorpopcount as xp
from onnx import TensorProto, helper
from qonnx.core.datatype import DataType
from qonnx.core.modelwrapper import ModelWrapper
from qonnx.custom_op.general.multithreshold import multithreshold
from qonnx.custom_op.registry import getCustomOp
from qonnx.transformation.general import (
    ApplyConfig,
    GiveReadableTensorNames,
    GiveUniqueNodeNames,
)
from qonnx.transformation.infer_datatypes import InferDataTypes
from qonnx.util.basic import (
    calculate_signed_dot_prod_range,
    gen_finn_dt_tensor,
    qonnx_make_model,
)

import finn.core.onnx_exec as oxe
import finn.transformation.fpgadataflow.convert_to_hw_layers as to_hw
from finn.analysis.fpgadataflow.exp_cycles_per_layer import exp_cycles_per_layer
from finn.analysis.fpgadataflow.hls_synth_res_estimation import hls_synth_res_estimation
from finn.transformation.fpgadataflow.compile_cppsim import CompileCppSim
from finn.transformation.fpgadataflow.create_stitched_ip import CreateStitchedIP
from finn.transformation.fpgadataflow.derive_characteristic import DeriveCharacteristic
from finn.transformation.fpgadataflow.hlssynth_ip import HLSSynthIP
from finn.transformation.fpgadataflow.minimize_accumulator_width import (
    MinimizeAccumulatorWidth,
)
from finn.transformation.fpgadataflow.minimize_weight_bit_width import (
    MinimizeWeightBitWidth,
)
from finn.transformation.fpgadataflow.prepare_cppsim import PrepareCppSim
from finn.transformation.fpgadataflow.prepare_ip import PrepareIP
from finn.transformation.fpgadataflow.prepare_rtlsim import PrepareRTLSim
from finn.transformation.fpgadataflow.set_exec_mode import SetExecMode
from finn.transformation.fpgadataflow.set_fifo_depths import InsertAndSetFIFODepths
from finn.transformation.fpgadataflow.specialize_layers import SpecializeLayers



def make_dynamic_matmul_modelwrapper(M, N, K, inp_A_t, inp_B_t):
    inp_A = [M, N]
    inp_B = [N, K]

    inp_A_tensor_value_info = helper.make_tensor_value_info("inp_A", TensorProto.FLOAT, inp_A)
    inp_B_tensor_value_info = helper.make_tensor_value_info("inp_B", TensorProto.FLOAT, inp_B)
    outp_tensor_value_info = helper.make_tensor_value_info("outp", TensorProto.FLOAT, [None, None])
    
    
    
    matmul_node = helper.make_node("MatMul", ["inp_A", "inp_B"], ["outp"])
    graph = helper.make_graph(
        nodes=[matmul_node], 
        name="matmul_graph_2_inputs", 
        inputs=[inp_A_tensor_value_info, inp_B_tensor_value_info], 
        outputs=[outp_tensor_value_info])

    model = qonnx_make_model(graph, producer_name="fclayer-model")
    model = ModelWrapper(model)

    model.set_tensor_datatype("inp_A", inp_A_t)
    model.set_tensor_datatype("inp_B", inp_B_t)
    model.set_tensor_datatype(
        "outp", DataType["INT32"]
    ) 
    return model




# matrix size [MxN] * [NxK]  
@pytest.mark.parametrize("M", [1, 32, 16])
@pytest.mark.parametrize("N", [1, 16, 64])
@pytest.mark.parametrize("K", [1, 8, 128])
# neuron folding, -1 is maximum possible
@pytest.mark.parametrize("nf", [-1, 2])
# synapse folding, -1 is maximum possible
@pytest.mark.parametrize("sf", [-1, 2])
@pytest.mark.parametrize("inp_A_t", [ DataType["UINT8"]])
@pytest.mark.parametrize("inp_B_t", [DataType["INT8"]])
@pytest.mark.fpgadataflow
@pytest.mark.slow
@pytest.mark.vivado
def test_fpgadataflow_rtl_dynamic_mvau(M, N, K, nf, sf, inp_A_t, inp_B_t):
    if nf == -1:
        nf = K
    if sf == -1:
        sf = N
    pe = K // nf    # Number of PEs fold columns
    simd = N // sf  # Number of SIMD lanes fold rows
    assert K % pe == 0
    assert N % sf == 0

    model = make_dynamic_matmul_modelwrapper(M, N, K, inp_A_t, inp_B_t)
    model.save("dynmvau.onnx")
    return 0
    model = model.transform(GiveUniqueNodeNames())
    model = model.transform(GiveReadableTensorNames())

    # Create MatMul & obtain golden reference output
    A = gen_finn_dt_tensor(
        model.get_tensor_datatype("inp_A"), model.get_tensor_shape("inp_A")
    )
    input_dict = prepare_inputs(A, idt, wdt, inp_name="global_in")

    # Execute ONNX model
    output_matmul = oxe.execute_onnx(model, input_dict)["global_out"]

    # Create MVAU (HLS)
    model = model.transform(to_hw.InferQuantizedMatrixVectorActivation())
    model = model.transform(GiveUniqueNodeNames())

    # Apply convert-to-rtl step
    model = model.transform(SpecializeLayers(part))
    model = model.transform(GiveUniqueNodeNames())

    # Apply folding (i.e. specify to use DSPs)
    folding_config = {
        "Defaults": {},
        "MVAU_rtl_0": {
            "PE": pe,
            "SIMD": simd,
            "resType": "dsp",
        },
    }
    model = model.transform(ApplyConfig(folding_config))
    model = model.transform(MinimizeWeightBitWidth())
    model = model.transform(MinimizeAccumulatorWidth())
    # make sure the changed datatypes are propagated through the network
    model = model.transform(InferDataTypes())

    # Run CPPsim
    model = model.transform(SetExecMode("cppsim"))
    model = model.transform(PrepareCppSim())
    model = model.transform(CompileCppSim())
    output_mvau_hls = oxe.execute_onnx(model, input_dict)["global_out"]
    assert (
        output_matmul == output_mvau_hls
    ).all(), "Output of ONNX model not matching output of node-by-node CPPsim!"

    # Run node-by-node RTLsim
    model = model.transform(SetExecMode("rtlsim"))
    model = model.transform(PrepareIP(part, clk_ns))
    model = model.transform(HLSSynthIP())
    model = model.transform(PrepareRTLSim())
    output_mvau_rtl = oxe.execute_onnx(model, input_dict)["global_out"]
    assert (
        output_matmul == output_mvau_rtl
    ).all(), "Output of ONNX model not matching output of node-by-node RTLsim!"

    # Run stitched-ip RTLsim
    model = model.transform(InsertAndSetFIFODepths(part, clk_ns))
    model = model.transform(PrepareIP(part, clk_ns))
    model = model.transform(HLSSynthIP())
    model = model.transform(CreateStitchedIP(part, clk_ns))

    model.set_metadata_prop("rtlsim_so", "")
    model.set_metadata_prop("exec_mode", "rtlsim")
    output_mvau_rtl_stitch = oxe.execute_onnx(model, input_dict)["global_out"]

    assert (
        output_matmul == output_mvau_rtl_stitch
    ).all(), "Output of ONNX model not matching output of stitched-IP RTL model!"
