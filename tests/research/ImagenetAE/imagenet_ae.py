# -*-coding: utf-8 -*-
"""
.. invisible:
     _   _ _____ _     _____ _____
    | | | |  ___| |   |  ___/  ___|
    | | | | |__ | |   | |__ \ `--.
    | | | |  __|| |   |  __| `--. \
    \ \_/ / |___| |___| |___/\__/ /
     \___/\____/\_____|____/\____/

Created on July 4, 2014

Model created for object recognition. Dataset - Imagenet.
Model - convolutional neural network, dynamically
constructed, with pretraining of all layers one by one with autoencoder.

███████████████████████████████████████████████████████████████████████████████

Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.

███████████████████████████████████████████████████████████████████████████████
"""


import json
import os
import pickle

import numpy
from zope.interface import implementer

from veles.config import root
import veles.error as error
from veles.memory import Vector
from veles.mutable import Bool
import veles.opencl_types as opencl_types
import veles.plotting_units as plotting_units
import veles.znicz.conv as conv
import veles.znicz.decision as decision
import veles.znicz.evaluator as evaluator
import veles.loader as loader
import veles.znicz.deconv as deconv
import veles.znicz.gd_deconv as gd_deconv
import veles.znicz.image_saver as image_saver
import veles.znicz.nn_plotting_units as nn_plotting_units
import veles.znicz.pooling as pooling
import veles.znicz.depooling as depooling
import veles.znicz.dropout as dropout
import veles.znicz.activation as activation
import veles.znicz.all2all as all2all
import veles.znicz.gd as gd
import veles.znicz.gd_pooling as gd_pooling
import veles.znicz.gd_conv as gd_conv
from veles.znicz.nn_rollback import NNRollback
from veles.znicz.nn_units import NNSnapshotter
from veles.znicz.standard_workflow import StandardWorkflowBase
from veles.mean_disp_normalizer import MeanDispNormalizer
from veles.units import IUnit, Unit
from veles.distributable import IDistributable
import veles.prng as prng
from veles.prng.uniform import Uniform
from veles.dummy import DummyWorkflow


@implementer(IUnit, IDistributable)
class Destroyer(Unit):
    """
    Modification of EndPoint Unit, created for Convolutional Autoencoder
    """
    def initialize(self, **kwargs):
        pass

    def run(self):
        if not self.is_slave:
            self.workflow.on_workflow_finished()

    def generate_data_for_master(self):
        return True

    def generate_data_for_slave(self, slave):
        return None

    def apply_data_from_master(self, data):
        pass

    def apply_data_from_slave(self, data, slave):
        if not bool(self.gate_block) and not bool(self.gate_skip):
            self.run()

    def drop_slave(self, slave):
        pass


@implementer(loader.ILoader)
class ImagenetAELoader(loader.Loader):
    MAPPING = "imagenet_ae_loader"
    """loads imagenet from samples.dat, labels.pickle"""
    def __init__(self, workflow, **kwargs):
        super(ImagenetAELoader, self).__init__(workflow, **kwargs)
        self.mean = Vector()
        self.rdisp = Vector()
        self.file_samples = ""
        self.sx = root.imagenet_ae.loader.sx
        self.sy = root.imagenet_ae.loader.sy

    def init_unpickled(self):
        super(ImagenetAELoader, self).init_unpickled()
        self.original_labels = None

    def initialize(self, **kwargs):
        self.normalizer.reset()
        super(ImagenetAELoader, self).initialize(**kwargs)
        self.minibatch_labels.reset(numpy.zeros(
            self.max_minibatch_size, dtype=numpy.int32))

    def __getstate__(self):
        state = super(ImagenetAELoader, self).__getstate__()
        state["original_labels"] = None
        state["file_samples"] = None
        return state

    def load_data(self):
        self.original_labels = []
        with open(root.imagenet_ae.loader.names_labels_filename, "rb") as fin:
            for lbl in pickle.load(fin):
                self.original_labels.append(int(lbl))
                self.labels_mapping[int(lbl)] = int(lbl)
        self.info("Labels (min max count): %d %d %d",
                  numpy.min(self.original_labels),
                  numpy.max(self.original_labels),
                  len(self.original_labels))

        with open(root.imagenet_ae.loader.count_samples_filename, "r") as fin:
            for i, n in enumerate(json.load(fin)):
                self.class_lengths[i] = n
        self.info("Class Lengths: %s", str(self.class_lengths))

        if self.total_samples != len(self.original_labels):
            raise error.Bug(
                "Number of labels missmatches sum of class lengths")

        with open(root.imagenet_ae.loader.matrixes_filename, "rb") as fin:
            matrixes = pickle.load(fin)

        self.mean.mem = matrixes[0]
        self.rdisp.mem = matrixes[1].astype(
            opencl_types.dtypes[root.common.precision_type])
        if numpy.count_nonzero(numpy.isnan(self.rdisp.mem)):
            raise ValueError("rdisp matrix has NaNs")
        if numpy.count_nonzero(numpy.isinf(self.rdisp.mem)):
            raise ValueError("rdisp matrix has Infs")
        if self.mean.shape != self.rdisp.shape:
            raise ValueError("mean.shape != rdisp.shape")
        if self.mean.shape[0] != self.sy or self.mean.shape[1] != self.sx:
            raise ValueError("mean.shape != (%d, %d)" % (self.sy, self.sx))

        self.file_samples = open(root.imagenet_ae.loader.samples_filename,
                                 "rb")
        if (self.file_samples.seek(0, 2) // (self.sx * self.sy * 4) !=
                len(self.original_labels)):
            raise error.Bug("Wrong data file size")

    def create_minibatch_data(self):
        sh = [self.max_minibatch_size]
        sh.extend(self.mean.shape)
        self.minibatch_data.mem = numpy.zeros(sh, dtype=numpy.uint8)

    def fill_indices(self, start_offset, count):
        self.minibatch_indices.map_invalidate()
        idxs = self.minibatch_indices.mem
        self.shuffled_indices.map_read()
        idxs[:count] = self.shuffled_indices[start_offset:start_offset + count]

        if self.is_master:
            return True

        self.minibatch_data.map_invalidate()
        self.minibatch_labels.map_invalidate()

        sample_bytes = self.mean.mem.nbytes

        for index, index_sample in enumerate(idxs[:count]):
            self.file_samples.seek(int(index_sample) * sample_bytes)
            self.file_samples.readinto(self.minibatch_data.mem[index])
            self.minibatch_labels.mem[index] = self.original_labels[
                int(index_sample)]

        if count < len(idxs):
            idxs[count:] = self.class_lengths[1]  # no data sample is there
            self.minibatch_data.mem[count:] = self.mean.mem
            self.minibatch_labels.mem[count:] = 0  # 0 is no data

        return True

    def fill_minibatch(self):
        raise error.Bug("Control should not go here")


class ImagenetAEWorkflow(StandardWorkflowBase):
    """Workflow.
    """
    def fix(self, unit, *attrs):
        fix = {}
        for attr in attrs:
            fix[attr] = id(getattr(unit, attr))
        self.fixed[unit] = fix

    def check_fixed(self):
        for unit, fix in self.fixed.items():
            for attr, addr in fix.items():
                if id(getattr(unit, attr)) != addr:
                    raise ValueError("Fixed attribute has changed: %s.%s" %
                                     (unit.__class__.__name__, attr))

    def init_unpickled(self):
        super(ImagenetAEWorkflow, self).init_unpickled()
        self.forward_map = {
            "conv": conv.Conv,
            "stochastic_abs_pooling": pooling.StochasticAbsPooling,
            "max_abs_pooling": pooling.MaxAbsPooling,
            "stochastic_pooling": pooling.StochasticPooling,
            "max_pooling": pooling.MaxPooling,
            "activation_mul": activation.ForwardMul,
            "all2all": all2all.All2All,
            "all2all_tanh": all2all.All2AllTanh,
            "activation_tanhlog": activation.ForwardTanhLog,
            "softmax": all2all.All2AllSoftmax,
            "dropout": dropout.DropoutForward}
        self.last_de_map = {  # If the last unit in ae-layer => replace with:
            pooling.StochasticAbsPooling:
            pooling.StochasticAbsPoolingDepooling,
            pooling.StochasticPooling: pooling.StochasticPoolingDepooling}
        self.last_de_unmap = {  # When adding next autoencoder => replace with:
            pooling.StochasticAbsPoolingDepooling:
            pooling.StochasticAbsPooling,
            pooling.StochasticPoolingDepooling: pooling.StochasticPooling}
        self.de_map = {
            conv.Conv: deconv.Deconv,
            pooling.StochasticAbsPooling: depooling.Depooling,
            pooling.MaxAbsPooling: depooling.Depooling,
            pooling.StochasticPooling: depooling.Depooling,
            pooling.MaxPooling: depooling.Depooling}
        self.gd_map = {
            deconv.Deconv: gd_deconv.GDDeconv,
            all2all.All2All: gd.GradientDescent,
            all2all.All2AllTanh: gd.GDTanh,
            all2all.All2AllSoftmax: gd.GDSoftmax,
            activation.ForwardTanhLog: activation.BackwardTanhLog,
            activation.ForwardMul: activation.BackwardMul,
            pooling.StochasticAbsPooling: gd_pooling.GDMaxAbsPooling,
            pooling.StochasticPooling: gd_pooling.GDMaxPooling,
            pooling.MaxAbsPooling: gd_pooling.GDMaxAbsPooling,
            pooling.MaxPooling: gd_pooling.GDMaxPooling,
            conv.Conv: gd_conv.GradientDescentConv,
            dropout.DropoutForward: dropout.DropoutBackward}
        self.fixed = {}

    def __init__(self, workflow, **kwargs):
        layers = kwargs.get("layers")
        device = kwargs.get("device")
        kwargs["layers"] = layers
        kwargs["device"] = device
        super(ImagenetAEWorkflow, self).__init__(workflow, **kwargs)

        self.slave_stats = plotting_units.SlaveStats(self)
        self.slave_stats.link_from(self.start_point)

        self.repeater.link_from(self.start_point)

        self.loader = ImagenetAELoader(
            self, minibatch_size=root.imagenet_ae.loader.minibatch_size)
        self.loader.link_from(self.repeater)
        self.fix(self.loader, "minibatch_data", "mean", "rdisp",
                 "class_lengths")

        self.meandispnorm = MeanDispNormalizer(self)
        self.meandispnorm.link_attrs(self.loader,
                                     ("input", "minibatch_data"),
                                     "mean", "rdisp")
        self.meandispnorm.link_from(self.loader)
        self.fix(self.meandispnorm, "input", "output", "mean", "rdisp")
        prev = self.meandispnorm

        ae = []
        ae_layers = []
        last_conv = None
        self.n_ae = 0
        in_ae = False
        self.uniform = None
        for layer in layers:
            if layer["type"] == "ae_begin":
                self.info("Autoencoder block begin")
                in_ae = True
                continue
            if layer["type"] == "ae_end":
                self.info("Autoencoder block end")
                self.info("One AE at a time, so skipping other layers")
                self.n_ae += 1
                break
            if layer["type"][:4] == "conv":
                if in_ae:
                    layer["include_bias"] = False
                layer["padding"] = deconv.Deconv.compute_padding(
                    self.loader.sx, self.loader.sy,
                    layer["kx"], layer["ky"], layer["sliding"])
            Forward = self.forward_map[layer["type"]]
            unit = Forward(self, **layer)
            unit.layer = dict(layer)
            if in_ae:
                ae.append(unit)
                ae_layers.append(layer)
            if isinstance(unit, pooling.StochasticPoolingBase):
                if self.uniform is None:
                    self.uniform = Uniform(DummyWorkflow(), num_states=512)
                unit.uniform = self.uniform
            self.forwards.append(unit)
            unit.link_from(prev)
            if isinstance(unit, conv.ConvolutionalBase):
                conv_ = unit
            unit.link_attrs(prev, ("input", "output"))
            if isinstance(unit, activation.ForwardMul):
                unit.link_attrs(prev, "output")
            self.fix(unit, "input", "output", "weights")
            prev = unit
            if layer["type"][:4] == "conv" and in_ae:
                last_conv = prev
        else:
            raise error.BadFormatError("No autoencoder layers found")

        if last_conv is None:
            raise error.BadFormatError("No convolutional layer found")

        de = []
        for i in range(len(ae) - 1, -1, -1):
            if (i == len(ae) - 1 and id(ae[-1]) == id(self.forwards[-1]) and
                    ae[-1].__class__ in self.last_de_map):
                self.info("Replacing pooling with pooling-depooling")
                De = self.last_de_map[ae[-1].__class__]
                unit = De(self, **ae_layers[i])
                unit.uniform = ae[-1].uniform
                unit.layer = ae[-1].layer
                unit.link_attrs(self.forwards[-2], ("input", "output"))
                del self.forwards[-1]
                ae[-1].unlink_all()
                self.del_ref(ae[-1])
                ae[-1] = unit
                self.forwards.append(unit)
                unit.link_from(self.forwards[-2])
                prev = unit
                self.fix(unit, "input", "uniform")
                de.append(unit)  # for the later assert to work
                continue
            De = self.de_map[ae[i].__class__]
            unit = De(self, **ae_layers[i])
            if isinstance(unit, deconv.Deconv):
                unit.link_conv_attrs(conv_)
                self.deconv = unit
                unit.unsafe_padding = True
            de.append(unit)
            self.forwards.append(unit)
            unit.link_from(prev)
            for dst_src in (("weights", "weights"),
                            ("output_shape_source", "input"),
                            ("output_offset", "input_offset")):
                if hasattr(unit, dst_src[0]):
                    unit.link_attrs(ae[i], dst_src)
            if isinstance(prev, pooling.StochasticPoolingDepooling):
                unit.link_attrs(prev, "input")
            else:
                unit.link_attrs(prev, ("input", "output"))
            self.fix(unit, "input", "weights", "output",
                     "output_shape_source")
            prev = unit

        assert len(ae) == len(de)

        # Add evaluator unit
        unit = evaluator.EvaluatorMSE(self)
        self.evaluator = unit
        unit.link_from(self.forwards[-1])
        unit.link_attrs(self.forwards[-1], "output")
        unit.link_attrs(self.loader, ("batch_size", "minibatch_size"))
        unit.link_attrs(self.meandispnorm, ("target", "output"))
        self.fix(self.evaluator, "output", "target", "err_output", "metrics")

        # Add decision unit
        unit = decision.DecisionMSE(
            self, fail_iterations=root.imagenet_ae.decision.fail_iterations,
            max_epochs=root.imagenet_ae.decision.max_epochs)
        self.decision = unit
        unit.link_from(self.evaluator)
        unit.link_attrs(self.loader, "minibatch_class",
                        "minibatch_size", "last_minibatch",
                        "class_lengths", "epoch_ended",
                        "epoch_number")
        unit.link_attrs(self.evaluator, ("minibatch_metrics", "metrics"))
        self.fix(self.decision, "minibatch_metrics", "class_lengths")

        unit = NNSnapshotter(
            self, prefix=root.imagenet_ae.snapshotter.prefix,
            directory=root.imagenet_ae.snapshotter.directory,
            compress="", time_interval=0, interval=1)
        self.snapshotter = unit
        unit.link_from(self.decision)
        unit.link_attrs(self.decision, ("suffix", "snapshot_suffix"))
        unit.gate_skip = ~self.loader.epoch_ended | ~self.decision.improved

        unit = NNRollback(self)
        self.rollback = unit
        unit.link_from(self.snapshotter)
        unit.improved = self.decision.train_improved
        unit.gate_skip = ~self.loader.epoch_ended | self.decision.complete

        # Add gradient descent unit
        GD = self.gd_map[self.forwards[-1].__class__]
        unit = GD(self, **ae_layers[0])
        if isinstance(unit, gd_deconv.GDDeconv):
            unit.link_attrs(
                self.deconv, "weights", "input", "hits", "n_kernels",
                "kx", "ky", "sliding", "padding", "unpack_data", "unpack_size")
        self.gds.append(unit)
        unit.link_attrs(self.evaluator, "err_output")
        unit.link_attrs(self.forwards[-1], "weights", "input")
        unit.gate_skip = self.decision.gd_skip
        self.fix(unit, "err_output", "weights", "input", "err_input")
        self.rollback.add_gd(unit)

        assert len(self.gds) == 1

        self.gds[0].need_err_input = False
        self.repeater.link_from(self.gds[0])

        prev = self.add_plotters(self.rollback, last_conv, ae[-1])

        self.gds[-1].link_from(prev)

        self.destroyer = Destroyer(self)

        self.add_end_point()

    def add_end_point(self):
        self.rollback.gate_block = Bool(False)
        self.rollback.gate_skip = (~self.loader.epoch_ended |
                                   self.decision.complete)
        self.end_point.unlink_all()
        self.end_point.link_from(self.gds[0])
        self.end_point.gate_block = ~self.decision.complete
        self.loader.gate_block = self.decision.complete
        if not hasattr(self, "destroyer"):
            self.destroyer = Destroyer(self)
        self.destroyer.unlink_all()
        self.destroyer.link_from(self.gds[0])
        self.destroyer.gate_block = ~self.decision.complete

        uu = None
        for u in self.start_point.links_to.keys():
            if isinstance(u, plotting_units.SlaveStats):
                uu = u
                break
        if uu is not None:
            self.warning("Found malicious plotter, will purge it")
            uu.unlink_before()
            self.del_ref(uu)
            del uu
            self.warning("Purge succeeded")

    def del_plotters(self):
        if hasattr(self, "plt"):
            for p in self.plt:
                p.unlink_all()
                self.del_ref(p)
            del self.plt
        if hasattr(self, "plt_mx"):
            self.plt_mx.unlink_all()
            self.del_ref(self.plt_mx)
            del self.plt_mx
        if hasattr(self, "plt_inp"):
            self.plt_inp.unlink_all()
            self.del_ref(self.plt_inp)
            del self.plt_inp
        if hasattr(self, "plt_out"):
            self.plt_out.unlink_all()
            self.del_ref(self.plt_out)
            del self.plt_out
        if hasattr(self, "plt_deconv"):
            self.plt_deconv.unlink_all()
            self.del_ref(self.plt_deconv)
            del self.plt_deconv

    def add_plotters(self, prev, last_conv, last_ae):
        if not self.is_standalone:
            return prev

        self.del_plotters()

        # MSE plotter
        self.plt = []
        styles = ["r-", "b-", "k-"]
        for i in range(1, 3):
            self.plt.append(plotting_units.AccumulatingPlotter(
                self, name="mse", plot_style=styles[i]))
            self.plt[-1].input = self.decision.epoch_metrics
            self.plt[-1].input_field = i
            self.plt[-1].link_from(prev)
            self.plt[-1].gate_skip = ~self.decision.epoch_ended
            prev = self.plt[-1]
        self.plt[0].clear_plot = True
        self.plt[-1].redraw_plot = True

        # Weights plotter
        self.plt_mx = nn_plotting_units.Weights2D(
            self, name="Conv Weights", limit=256, split_channels=False)
        self.plt_mx.link_attrs(last_conv, ("input", "weights"))
        self.plt_mx.get_shape_from = [last_conv.kx, last_conv.ky,
                                      last_conv.input]
        self.plt_mx.link_from(prev)
        self.plt_mx.gate_skip = ~self.decision.epoch_ended
        prev = self.plt_mx

        # Input plotter
        self.plt_inp = nn_plotting_units.Weights2D(
            self, name="Conv Input", limit=64, split_channels=False)
        self.plt_inp.link_attrs(last_conv, "input")
        self.plt_inp.link_from(prev)
        self.plt_inp.gate_skip = ~self.decision.epoch_ended
        prev = self.plt_inp

        # Output plotter
        self.plt_out = nn_plotting_units.Weights2D(
            self, name="Output", limit=256, split_channels=False)
        if isinstance(last_ae, pooling.StochasticPoolingDepooling):
            self.plt_out.link_attrs(last_ae, "input")
        else:
            self.plt_out.link_attrs(last_ae, ("input", "output"))
        self.plt_out.link_from(prev)
        self.plt_out.gate_skip = ~self.decision.epoch_ended
        prev = self.plt_out

        # Deconv result plotter
        self.plt_deconv = nn_plotting_units.Weights2D(
            self, name="Deconv result", limit=64, split_channels=False)
        self.plt_deconv.link_attrs(self.forwards[-1], ("input", "output"))
        self.plt_deconv.link_from(prev)
        self.plt_deconv.gate_skip = ~self.decision.epoch_ended
        prev = self.plt_deconv

        return prev

    def initialize(self, device, **kwargs):
        if (self.forwards[0].weights.mem is not None and
                root.imagenet_ae.from_snapshot_add_layer):
            self.info("Restoring from snapshot detected, "
                      "will adjust the workflow")
            self.adjust_workflow()
            self.info("Workflow adjusted, will initialize now")
        else:
            self.decision.max_epochs += root.imagenet_ae.decision.max_epochs
        self.decision.complete <<= False
        self.info("Set decision.max_epochs to %d and complete=False",
                  self.decision.max_epochs)
        super(ImagenetAEWorkflow, self).initialize(device, **kwargs)
        self.check_fixed()
        self.dump_shapes()

    def dump_shapes(self):
        self.info("Input-Output Shapes:")
        for i, fwd in enumerate(self.forwards):
            self.info("%d: %s: %s => %s", i, repr(fwd),
                      str(fwd.input.shape) if fwd.input else "None",
                      str(fwd.output.shape) if fwd.output else "None")

    def switch_to_fine_tuning(self):
        if len(self.gds) == len(self.forwards):
            self.info("Already at fine-tune stage, will exit with code 1 now")
            os._exit(1)
        # Add gradient descent units for the remaining forward units
        self.gds[0].unlink_after()
        self.gds[0].need_err_input = True
        prev = self.gds[0]

        pool = None
        for i in range(len(self.forwards) - len(self.gds) - 1, -1, -1):
            GD = self.gd_map[self.forwards[i].__class__]
            if hasattr(self.forwards[i], "layer"):
                kwargs = dict(self.forwards[i].layer)
            if isinstance(self.forwards[i], pooling.StochasticPoolingBase):
                pool = self.forwards[i]
            if isinstance(self.forwards[i], conv.Conv):
                conv_ = self.forwards[i]
            for attr in ("n_kernels", "kx", "ky", "sliding", "padding",
                         "factor", "include_bias"):
                vle = getattr(self.forwards[i], attr, None)
                if vle is not None:
                    kwargs[attr] = vle
            if "learning_rate_ft" in kwargs:
                kwargs["learning_rate"] = kwargs["learning_rate_ft"]
            if "learning_rate_ft_bias" in kwargs:
                kwargs["learning_rate_bias"] = kwargs["learning_rate_ft_bias"]
            unit = GD(self, **kwargs)
            self.gds.insert(0, unit)
            unit.link_from(prev)
            if isinstance(unit, gd_pooling.GDPooling) and pool is not None:
                unit.link_attrs(pool, "kx", "ky", "sliding")
            if isinstance(unit, gd_conv.GradientDescentConv):
                unit.link_attrs(
                    conv_, "n_kernels", "kx", "ky", "sliding", "padding",
                    "unpack_data", "unpack_size")
            unit.link_attrs(prev, ("err_output", "err_input"))
            unit.link_attrs(self.forwards[i], "weights", "input", "output")
            if hasattr(self.forwards[i], "input_offset"):
                unit.link_attrs(self.forwards[i], "input_offset")
            if hasattr(self.forwards[i], "mask"):
                unit.link_attrs(self.forwards[i], "mask")
            if self.forwards[i].bias is not None:
                unit.link_attrs(self.forwards[i], "bias")
            unit.gate_skip = self.decision.gd_skip
            prev = unit
            self.fix(unit, "weights", "input", "output",
                     "err_input", "err_output")

        self.gds[0].need_err_input = False
        self.repeater.link_from(self.gds[0])

        self.rollback.reset()
        noise = float(root.imagenet_ae.fine_tuning_noise)
        for unit in self.gds:
            if not isinstance(unit, activation.Activation):
                self.rollback.add_gd(unit)
            if not noise:
                continue
            if unit.weights:
                a = unit.weights.plain
                a += prng.get().normal(0, noise, unit.weights.size)
            if unit.bias:
                a = unit.bias.plain
                a += prng.get().normal(0, noise, unit.bias.size)

        # Reset last best error, `cause we have modified the workflow
        self.decision.min_validation_n_err = 1.0e30
        self.decision.min_train_validation_n_err = 1.0e30
        self.decision.min_train_n_err = 1.0e30

        self.decision.max_epochs += root.imagenet_ae.decision.max_epochs * 10

        self.add_end_point()

        # self.reset_weights()

    def reset_weights(self):
        for unit in self:
            if not hasattr(unit, "layer"):
                continue
            if hasattr(unit, "weights") and unit.weights:
                self.info("RESETTING weights for %s", repr(unit))
                a = unit.weights.plain
                a *= 0
                a += prng.get().normal(0, unit.layer["weights_stddev"],
                                       unit.weights.size)
            if hasattr(unit, "bias") and unit.bias:
                self.info("RESETTING bias for %s", repr(unit))
                a = unit.bias.plain
                a *= 0
                a += prng.get().normal(0, unit.layer["bias_stddev"],
                                       unit.bias.size)

    def create_output(self, unit):
        unit._batch_size = unit.input.shape[0]
        unit._sy = unit.input.shape[1]
        unit._sx = unit.input.shape[2]
        unit._n_channels = unit.input.size // (unit._batch_size *
                                               unit._sx * unit._sy)

        last_x = unit._sx - unit.kx
        last_y = unit._sy - unit.ky
        if last_x % unit.sliding[0] == 0:
            unit._out_sx = last_x // unit.sliding[0] + 1
        else:
            unit._out_sx = last_x // unit.sliding[0] + 2
        if last_y % unit.sliding[1] == 0:
            unit._out_sy = last_y // unit.sliding[1] + 1
        else:
            unit._out_sy = last_y // unit.sliding[1] + 2
            unit._output_size = (
                unit._n_channels * unit._out_sx *
                unit._out_sy * unit._batch_size)
            unit._output_shape = (
                unit._batch_size, unit._out_sy, unit._out_sx, unit._n_channels)
        if (not unit._no_output and
                (not unit.output or unit.output.size != unit._output_size)):
            unit.output.reset()
            unit.output.mem = numpy.zeros(
                unit.output_shape, dtype=unit.input.dtype)

    def adjust_workflow(self):
        self.info("Will extend %d autoencoder layers", self.n_ae)

        layers = root.imagenet_ae.layers
        n_ae = 0
        i_layer = 0
        i_fwd = 0
        i_fwd_last = 0
        for layer in layers:
            i_layer += 1
            if layer["type"] == "ae_begin":
                continue
            if layer["type"] == "ae_end":
                i_fwd_last = i_fwd
                n_ae += 1
                if n_ae >= self.n_ae:
                    break
                continue
            i_fwd += 1
        else:
            self.warning("Will switch to the fine-tuning task")
            return self.switch_to_fine_tuning()

        i_fwd = i_fwd_last
        for i in range(i_fwd, len(self.forwards)):
            self.forwards[i].unlink_all()
            self.del_ref(self.forwards[i])
        del self.forwards[i_fwd:]
        last_fwd = self.forwards[-1]
        prev = last_fwd

        if prev.__class__ in self.last_de_unmap:
            self.info("Replacing pooling-depooling with pooling")
            Forward = self.last_de_unmap[prev.__class__]
            layer = prev.layer
            uniform = prev.uniform
            prev.unlink_all()
            self.del_ref(prev)
            unit = Forward(self, **layer)
            unit.layer = layer
            self.forwards[-1] = unit
            unit.uniform = uniform
            unit.link_from(self.forwards[-2])
            unit.link_attrs(self.forwards[-2], ("input", "output"))
            self.create_output(unit)
            self.fix(unit, "input", "output", "input_offset", "uniform")
            prev = unit
            last_fwd = unit

        ae = []
        ae_layers = []
        last_conv = None
        in_ae = False
        for layer in layers[i_layer:]:
            if layer["type"] == "ae_begin":
                self.info("Autoencoder block begin")
                in_ae = True
                continue
            if layer["type"] == "ae_end":
                self.info("Autoencoder block end")
                self.info("One AE at a time, so skipping other layers")
                self.n_ae += 1
                break
            if layer["type"][:4] == "conv":
                if in_ae:
                    layer["include_bias"] = False
                layer["padding"] = deconv.Deconv.compute_padding(
                    last_fwd.output.shape[2], last_fwd.output.shape[1],
                    layer["kx"], layer["ky"], layer["sliding"])
                self.info("Computed padding for (kx, ky)=(%d, %d) "
                          "sliding=%s is %s", layer["kx"], layer["ky"],
                          str(layer["sliding"]), str(layer["padding"]))
            Forward = self.forward_map[layer["type"]]
            unit = Forward(self, **layer)
            unit.layer = dict(layer)
            if in_ae:
                ae.append(unit)
                ae_layers.append(layer)
            self.forwards.append(unit)
            unit.link_from(prev)
            if isinstance(unit, conv.ConvolutionalBase):
                conv_ = unit
            unit.link_attrs(prev, ("input", "output"))
            if isinstance(unit, activation.ForwardMul):
                unit.link_attrs(prev, "output")
            if isinstance(unit, pooling.StochasticPoolingBase):
                unit.uniform = self.uniform
            self.fix(unit, "input", "output", "weights")
            prev = unit
            if layer["type"][:4] == "conv" and in_ae:
                last_conv = prev

        if last_conv is None and in_ae:
            raise error.BadFormatError("No convolutional layer found")

        if in_ae:
            de = []
            for i in range(len(ae) - 1, -1, -1):
                if (i == len(ae) - 1 and id(ae[-1]) == id(self.forwards[-1])
                        and ae[-1].__class__ in self.last_de_map):
                    self.info("Replacing pooling with pooling-depooling")
                    De = self.last_de_map[ae[-1].__class__]
                    unit = De(self, **ae_layers[i])
                    unit.uniform = ae[-1].uniform
                    unit.layer = ae[-1].layer
                    unit.link_attrs(self.forwards[-2], ("input", "output"))
                    del self.forwards[-1]
                    ae[-1].unlink_all()
                    self.del_ref(ae[-1])
                    ae[-1] = unit
                    self.forwards.append(unit)
                    unit.link_from(self.forwards[-2])
                    prev = unit
                    self.fix(unit, "input", "uniform")
                    continue
                De = self.de_map[ae[i].__class__]
                unit = De(self, **ae_layers[i])
                if isinstance(unit, deconv.Deconv):
                    unit.link_conv_attrs(conv_)
                    self.deconv = unit
                    unit.unsafe_padding = True
                de.append(unit)
                self.forwards.append(unit)
                unit.link_from(prev)
                for dst_src in (("weights", "weights"),
                                ("output_shape_source", "input"),
                                ("output_offset", "input_offset")):
                    if hasattr(unit, dst_src[0]):
                        unit.link_attrs(ae[i], dst_src)
                if isinstance(prev, pooling.StochasticPoolingDepooling):
                    unit.link_attrs(prev, "input")
                else:
                    unit.link_attrs(prev, ("input", "output"))
                self.fix(unit, "input", "weights", "output",
                         "output_shape_source")
                prev = unit

            unit = self.evaluator
            unit.link_from(self.forwards[-1])
            unit.link_attrs(self.forwards[-1], "output")
            unit.link_attrs(last_conv, ("target", "input"))
            self.fix(self.evaluator, "output", "target", "err_output",
                     "metrics")

            assert len(self.gds) == 1

            self.gds[0].unlink_all()
            self.del_ref(self.gds[0])
            del self.gds[:]

            # Add gradient descent unit
            GD = self.gd_map[self.forwards[-1].__class__]
            unit = GD(self, **ae_layers[0])
            if isinstance(unit, gd_deconv.GDDeconv):
                unit.link_attrs(
                    self.deconv, "weights", "input", "hits", "n_kernels",
                    "kx", "ky", "sliding", "padding", "unpack_data",
                    "unpack_size")
            self.gds.append(unit)
            unit.link_attrs(self.evaluator, "err_output")
            unit.link_attrs(self.forwards[-1], "weights", "input")
            unit.gate_skip = self.decision.gd_skip
            self.fix(unit, "err_output", "weights", "input", "err_input")
            self.rollback.reset()
            self.rollback.add_gd(unit)

            assert len(self.gds) == 1

            self.gds[0].need_err_input = False
            self.repeater.link_from(self.gds[0])

            prev = self.add_plotters(self.rollback, last_conv, ae[-1])

            self.gds[-1].link_from(prev)

            # Reset last best error, `cause we have extended the workflow
            self.decision.min_validation_mse = 1.0e30
            self.decision.min_train_validation_mse = 1.0e30
            self.decision.min_train_mse = 1.0e30
            self.decision.min_validation_n_err = 1.0e30
            self.decision.min_train_validation_n_err = 1.0e30
            self.decision.min_train_n_err = 1.0e30

            self.decision.max_epochs += root.imagenet_ae.decision.max_epochs

        else:
            self.info("No more autoencoder levels, "
                      "will switch to the classification task")
            self.n_ae += 1
            # Add Image Saver unit
            self.image_saver = image_saver.ImageSaver(
                self, out_dirs=root.imagenet_ae.image_saver.out_dirs)
            self.image_saver.link_from(self.forwards[-1])
            self.image_saver.link_attrs(self.forwards[-1], "output", "max_idx")
            self.image_saver.link_attrs(
                self.loader,
                ("indices", "minibatch_indices"),
                ("labels", "minibatch_labels"),
                "minibatch_class", "minibatch_size")
            self.image_saver.link_attrs(self.meandispnorm, ("input", "output"))
            # Add evaluator unit
            self.evaluator.unlink_all()
            self.del_ref(self.evaluator)
            unit = evaluator.EvaluatorSoftmax(self)
            self.evaluator = unit
            unit.link_from(self.image_saver)
            unit.link_attrs(self.forwards[-1], "output", "max_idx")
            unit.link_attrs(self.loader, ("labels", "minibatch_labels"),
                            ("batch_size", "minibatch_size"))
            self.fix(self.evaluator, "output", "max_idx",
                     "labels", "err_output")

            # Add decision unit
            max_epochs = self.decision.max_epochs
            self.decision.unlink_all()
            self.del_ref(self.decision)
            unit = decision.DecisionGD(
                self,
                fail_iterations=root.imagenet_ae.decision.fail_iterations,
                max_epochs=max_epochs)
            self.decision = unit
            unit.link_from(self.evaluator)
            unit.link_attrs(self.loader, "minibatch_class",
                            "minibatch_size", "last_minibatch",
                            "class_lengths", "epoch_ended",
                            "epoch_number")
            unit.link_attrs(self.evaluator, ("minibatch_n_err", "n_err"),
                            ("minibatch_confusion_matrix", "confusion_matrix"),
                            ("minibatch_max_err_y_sum", "max_err_output_sum"))
            self.fix(self.decision, "minibatch_n_err", "class_lengths")

            unit = self.snapshotter
            unit.link_from(self.decision)
            unit.link_attrs(self.decision, ("suffix", "snapshot_suffix"))
            unit.gate_skip = ~self.loader.epoch_ended | ~self.decision.improved
            self.image_saver.gate_skip = ~self.decision.improved
            self.image_saver.link_attrs(self.snapshotter,
                                        ("this_save_time", "time"))

            self.rollback.gate_skip = (~self.loader.epoch_ended |
                                       self.decision.complete)
            self.rollback.improved = self.decision.train_improved

            assert len(self.gds) == 1

            self.gds[0].unlink_all()
            self.del_ref(self.gds[0])
            del self.gds[:]

            # Add gradient descent units
            self.rollback.reset()
            prev_gd = self.evaluator
            prev = None
            gds = []
            for i in range(len(self.forwards) - 1, i_fwd - 1, -1):
                GD = self.gd_map[self.forwards[i].__class__]
                kwargs = dict(self.forwards[i].layer)
                for attr in ("n_kernels", "kx", "ky", "sliding", "padding",
                             "factor", "include_bias"):
                    vle = getattr(self.forwards[i], attr, None)
                    if vle is not None:
                        kwargs[attr] = vle
                unit = GD(self, **kwargs)
                gds.append(unit)
                if prev is not None:
                    unit.link_from(prev)
                if isinstance(prev_gd, evaluator.EvaluatorBase):
                    unit.link_attrs(prev_gd, "err_output")
                else:
                    unit.link_attrs(prev_gd, ("err_output", "err_input"))
                unit.link_attrs(self.forwards[i], "weights", "input", "output")
                if hasattr(self.forwards[i], "input_offset"):
                    unit.link_attrs(self.forwards[i], "input_offset")
                if hasattr(self.forwards[i], "mask"):
                    unit.link_attrs(self.forwards[i], "mask")
                if self.forwards[i].bias is not None:
                    unit.link_attrs(self.forwards[i], "bias")
                unit.gate_skip = self.decision.gd_skip
                prev_gd = unit
                prev = unit
                self.fix(unit, "weights", "bias", "input", "output",
                         "err_input", "err_output")
                if not isinstance(unit, activation.Activation):
                    self.rollback.add_gd(unit)
            # Strip gd's without weights
            for i in range(len(gds) - 1, -1, -1):
                if (isinstance(gds[i], gd.GradientDescent) or
                        isinstance(gds[i], gd_conv.GradientDescentConv)):
                    break
                unit = gds.pop(-1)
                unit.unlink_all()
                self.del_ref(unit)
                del self.fixed[unit]
            for _ in gds:
                self.gds.append(None)
            for i, _gd in enumerate(gds):
                self.gds[-(i + 1)] = _gd
            del gds

            self.gds[0].need_err_input = False
            self.repeater.link_from(self.gds[0])

            prev = self.rollback

            if self.is_standalone:
                self.del_plotters()

                # Error plotter
                self.plt = []
                styles = ["r-", "b-", "k-"]
                for i in range(1, 3):
                    self.plt.append(plotting_units.AccumulatingPlotter(
                        self, name="Errors", plot_style=styles[i]))
                    self.plt[-1].input = self.decision.epoch_n_err_pt
                    self.plt[-1].input_field = i
                    self.plt[-1].link_from(prev)
                    self.plt[-1].gate_skip = ~self.decision.epoch_ended
                    prev = self.plt[-1]
                self.plt[0].clear_plot = True
                self.plt[-1].redraw_plot = True

                # Output plotter
                self.plt_out = nn_plotting_units.Weights2D(
                    self, name="Output", limit=256, split_channels=False)
                self.plt_out.link_attrs(self.forwards[i_fwd_last], "input")
                self.plt_out.link_from(prev)
                self.plt_out.gate_skip = ~self.decision.epoch_ended
                prev = self.plt_out

            self.gds[-1].link_from(prev)

            self.decision.max_epochs += 15

        self.add_end_point()


def run(load, main):
    IMAGENET_BASE_PATH = root.imagenet_ae.loader.path
    root.imagenet_ae.snapshotter.prefix = (
        "imagenet_ae_%s" % root.imagenet_ae.loader.year)
    CACHED_DATA_FNME = os.path.join(IMAGENET_BASE_PATH,
                                    str(root.imagenet_ae.loader.year))
    root.imagenet_ae.loader.names_labels_filename = os.path.join(
        CACHED_DATA_FNME, "original_labels_%s_%s_0.pickle" %
        (root.imagenet_ae.loader.year, root.imagenet_ae.loader.series))
    root.imagenet_ae.loader.count_samples_filename = os.path.join(
        CACHED_DATA_FNME, "count_samples_%s_%s_0.json" %
        (root.imagenet_ae.loader.year, root.imagenet_ae.loader.series))
    root.imagenet_ae.loader.samples_filename = os.path.join(
        CACHED_DATA_FNME, "original_data_%s_%s_0.dat" %
        (root.imagenet_ae.loader.year, root.imagenet_ae.loader.series))
    root.imagenet_ae.loader.matrixes_filename = os.path.join(
        CACHED_DATA_FNME, "matrixes_%s_%s_0.pickle" %
        (root.imagenet_ae.loader.year, root.imagenet_ae.loader.series))
    load(ImagenetAEWorkflow,
         layers=root.imagenet_ae.layers,
         loader_name=root.imagenet_ae.loader_name,
         loader_config={})
    main()