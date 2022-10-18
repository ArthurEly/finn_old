# Copyright (c) 2020, Xilinx
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

import os
from qonnx.core.datatype import DataType

from finn.custom_op.fpgadataflow.hlscustomop import HLSCustomOp

"""@package thresholding_binary_search
- ONNX i/o tensor shape assumptions for Thresholding:
- input 0 is the input tensor, shape (..., NumChannels)
- input 1 is the threshold tensor, shape (NumChannels, n_thres)
- output 0 is the output tensor, shape (..., NumChannels) - same as input
- the '...' here can be any shape (representing groups of vectors)

This module creates an RTL IP, HLS is not supported. See 'thresholding_batch'
for a HLS equivalent.
"""


class Thresholding_Bin_Search(HLSCustomOp):
    """Class that corresponds to finn-rtllib 'thresholding' function."""

    def __init__(self, onnx_node):
        super().__init__(onnx_node)

    def get_nodeattr_types(self):
        my_attrs = {
            # parallelization; channels thresholded per cycle
            "PE": ("i", True, 0),
            # number of channels (each may have different thresholds)
            "NumChannels": ("i", True, 0),
            # number of steps in thresholding function. Used only in decoupled mode
            "numSteps": ("i", True, 1),
            # string defining memory type
            "ram_style": ("s", False, "distributed", {"distributed", "block"}),
            # FINN DataTypes for inputs, outputs
            "inputDataType": ("s", True, ""),
            "weightDataType": ("s", True, ""),
            "outputDataType": ("s", True, ""),
            # input and output FIFO depths
            "inFIFODepth": ("i", False, 0),
            "outFIFODepth": ("i", False, 0),
            # number of input vectors, examples:
            # [1] is a single vector (like a FC layer with batch=1)
            # [4] is four vectors (like a FC layer with batch=4)
            # [1, 4, 4] is four * four vectors (like a conv layer with batch=1)
            "numInputVectors": ("ints", False, [1]),
            # memory mode for the thresholds
            # const -- embedded thresholds, default
            # decoupled -- streaming thresholds with streamer packaged inside IP
            "mem_mode": ("s", False, "const", {"const", "decoupled"}),
            # (mem_mode = decoupled only) whether weights (thresholds) will be
            # writable through an AXI-lite interface during runtime
            # 1 for enabled, 0 for disabled.
            # see finn-rtllib/memstream/doc/README for more about the memory
            # address map used for writable weights
            # IMPORTANT: After using AXI lite to either read or write the weights,
            # always "flush" the accelerator by first passing a dummy input
            # vector through the accelerator. This will get rid of any old
            # weight data from the weight FIFOs.
            "runtime_writeable_weights": ("i", False, 0, {0, 1}),
            "gen_top_module": ("s", False, ""),
        }
        my_attrs.update(super().get_nodeattr_types())
        return my_attrs

    def calc_tmem(self):
        num_channels = self.get_nodeattr("NumChannels")
        pe = self.get_nodeattr("PE")
        return num_channels // pe

    def make_shape_compatible_op(self, model):
        return []

    def infer_node_datatype(self, model):
        return

    def verify_node(self):
        return []

    def bram_estimation(self):
        return 0

    def lut_estimation(self):
        return 0

    def get_input_datatype(self):
        return DataType[self.get_nodeattr("inputDataType")]

    def get_output_datatype(self):
        return DataType[self.get_nodeattr("outputDataType")]

    def get_weight_datatype(self):
        """The term 'weights' and 'thresholds' are used interchangably in this class."""
        return DataType[self.get_nodeattr("weightDataType")]

    def minimize_accumulator_width(self, model):
        return None

    def get_instream_width(self):
        i_bits = self.get_input_datatype().bitwidth()
        return i_bits * self.get_nodeattr("PE")

    def get_outstream_width(self):
        o_bits = self.get_output_datatype().bitwidth()
        return o_bits * self.get_nodeattr("PE")

    def get_weightstream_width(self):
        """Returns weight stream width. Used only in decoupled mode."""
        if self.get_nodeattr("mem_mode") == "decoupled":
            pe = self.get_nodeattr("PE")
            wp = self.get_weight_datatype().bitwidth()
            n_thres_steps = self.get_nodeattr("numSteps")
            w_width = pe * wp * n_thres_steps
            return w_width
        else:
            return 0

    def get_folded_input_shape(self):
        fold = self.calc_tmem()
        pe = self.get_nodeattr("PE")
        vecs = list(self.get_nodeattr("numInputVectors"))
        folded_input_shape = tuple(vecs + [fold, pe])
        return folded_input_shape

    def get_folded_output_shape(self):
        # same shape as input
        return self.get_folded_input_shape()

    def get_normal_input_shape(self):
        num_channels = self.get_nodeattr("NumChannels")
        vecs = list(self.get_nodeattr("numInputVectors"))
        normal_input_shape = tuple(vecs + [num_channels])
        return normal_input_shape

    def get_normal_output_shape(self):
        # same shape as input
        return self.get_normal_input_shape()

    def get_number_output_values(self):
        return 0

    def get_exp_cycles(self):
        return 0

    def get_template_param_values(self):
        return dict()

    def make_weight_file(self, weights, weight_file_mode, weight_file_name):
        """Produce a file containing given weights (thresholds) in appropriate
        format for this layer. This file can be used for either synthesis or
        run-time reconfig of weights.

        Arguments:
        * weights : numpy array with weights to be put into the file
        * weight_file_mode : one of {hls_header, decoupled_verilog_dat,
          decoupled_runtime}
        * weight_file_name : filename for the weight file to be generated
        """
        return

    # Get the integer from the DataType and string-ify it
    # This assumes that the data is in the form "INTx" or similar
    def conv_datatype_to_str(self, data_type):
        # Handle the case that an int is passed to the function
        if isinstance(data_type, int):
            return str(data_type)
        return str(DataType[data_type].bitwidth())

    def prepare_codegen_rtl_values(self):
        code_gen_dict = {}

        # Identify the module names
        code_gen_dict["$MODULE_NAME$"] = [self.get_verilog_top_module_name()]
        code_gen_dict["$MODULE_NAME_AXI$"] = [self.get_verilog_top_module_name() + "_axi"]
        code_gen_dict["$MODULE_NAME_AXI_WRAPPER$"] = [self.get_verilog_top_module_name() + "_axi_wrapper"]
        # The AXI wrapper is the top module:
        code_gen_dict["$TOP_MODULE$"] = code_gen_dict["$MODULE_NAME_AXI_WRAPPER$"]

        # Identify the module variables
        output_data_type = self.get_nodeattr("outputDataType") # output precision
        input_data_type = self.get_nodeattr("weightDataType") # input/threshold precision
        num_channels = self.get_nodeattr("NumChannels") # number of channels

        code_gen_dict["$N$"] = [self.conv_datatype_to_str(output_data_type)] # output precision
        code_gen_dict["$M$"] = [self.conv_datatype_to_str(input_data_type)] # input/threshold precision
        code_gen_dict["$C$"] = [self.conv_datatype_to_str(num_channels)] # number of channels

        return code_gen_dict

    def get_rtl_file_list(self):
        return ["thresholding.sv",
                "thresholding_axi.sv",
                "thresholding_axi_wrapper.v"]

    def get_rtl_file_paths(self):
        rtl_root_dir = os.environ["FINN_ROOT"] + "/finn-rtllib/thresholding/hdl/"
        rtl_file_list = self.get_rtl_file_list()
        rtl_file_paths = [rtl_root_dir + x for x in rtl_file_list]
        return rtl_file_paths

    def get_rtl_template_data(self, path):
        with open(path, "r") as f:
            template = f.read()
        return template

    def fill_in_rtl_template_data(self, replace_dict, template_data):
        template_data_cp = template_data
        for key in replace_dict:
            # transform list into long string separated by '\n'
            replacement_line = "\n".join(replace_dict[key])
            template_data_cp = template_data_cp.replace(key, replacement_line)
        return template_data_cp

    def dump_rtl_data(self, dest_dir, filename, data):
        with open(os.path.join(dest_dir, filename), "w") as f:
            f.write(data)
        return

    def generate_hdl(self):
        # Generate a dictionary of values to put in RTL schema
        code_gen_dict = self.prepare_codegen_rtl_values()

        # Retrieve the destination directory for the final RTL files
        code_gen_dir = self.get_nodeattr("code_gen_dir_ipgen")

        for rtl_file_path in self.get_rtl_file_paths():
            # read in original file
            template_data = self.get_rtl_template_data(rtl_file_path)
            # apply code generation to templates
            data = self.fill_in_rtl_template_data(code_gen_dict, template_data)
            # dump to dest dir
            file_only_path = rtl_file_path.split('/')[-1]
            self.dump_rtl_data(code_gen_dir, file_only_path, data)

        self.set_nodeattr("gen_top_module", code_gen_dict["$TOP_MODULE$"][0])
        return

    def code_generation_ipgen(self, model, fpgapart, clk):
        # Take RTL templates and fill in appropriate values
        self.generate_hdl()

        # Generate params for RTLSim
        code_gen_dir = self.get_nodeattr("code_gen_dir_ipgen")
        self.generate_params(model, code_gen_dir)

        # set ipgen_path and ip_path so that HLS-Synth transformation
        # and stich_ip transformation do not complain
        # i.e. during the HLSSynthIP() transformation
        self.set_nodeattr("ipgen_path", code_gen_dir)
        self.set_nodeattr("ip_path", code_gen_dir)

    def generate_params(self, model, path):
        return

    def execute_node(self, context, graph):
        return

    def code_generation_ipi(self):
        """Constructs and returns the TCL for node instantiation in Vivado IPI."""
        code_gen_dir = self.get_nodeattr("code_gen_dir_ipgen")
        rtl_file_list = self.get_rtl_file_list()

        cmd = [
            "add_files -norecurse %s"
            % (
                os.path.join(
                    code_gen_dir, rtl_file_list[0]
                )
            ),
            "add_files -norecurse %s"
            % (
                os.path.join(
                    code_gen_dir, rtl_file_list[1]
                )
            ),
            "add_files -norecurse %s"
            % (
                os.path.join(
                    code_gen_dir, rtl_file_list[2]
                )
            ),
            "create_bd_cell -type module -reference %s %s"
            % (self.get_nodeattr("gen_top_module"), self.onnx_node.name),

            # Fixme - these settings are temporary to prevent the following errors:
            # ERROR: [BD 41-237] Bus Interface property FREQ_HZ does not match between /Thresholding_Binary_Search_0/s_axis(100000000) and /StreamingFIFO_0/out_V(200000000.000000)
            # ERROR: [BD 41-237] Bus Interface property FREQ_HZ does not match between /StreamingFIFO_1/in0_V(200000000.000000) and /Thresholding_Binary_Search_0/m_axis(100000000)
            "set_property -dict [list CONFIG.FREQ_HZ {200000000}] [get_bd_intf_pins Thresholding_Binary_Search_0/s_axis]",
            "set_property -dict [list CONFIG.FREQ_HZ {200000000}] [get_bd_intf_pins Thresholding_Binary_Search_0/m_axis]",
        ]

        return cmd

    def global_includes(self):
        pass

    def defines(self, var):
        pass

    def read_npy_data(self):
        pass

    def strm_decl(self):
        pass

    def docompute(self):
        pass

    def dataoutstrm(self):
        pass

    def save_as_npy(self):
        pass

    def blackboxfunction(self):
        pass

    def pragmas(self):
        pass
