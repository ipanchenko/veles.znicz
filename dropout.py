"""
Created on Apr 25, 2014

A dropout layer. It is a signal repeater with some repeating channels set to 0.
Inputs to be disabled are randomly selected each forward proparation.

Detailed description given in article by Krizhevsky, Sutskever and Hinton:
"ImageNet Classification with Deep Convolutional Neural Networks" (sec. 4.2).
"""

import numpy as np

from veles import config, formats, OpenCLUnit
import veles.rnd as rnd


class Dropout(OpenCLUnit):
    """
    A base class for forward and backward units of local
    response normalization.
    """
    def __init__(self, workflow, **kwargs):
        super(Dropout, self).__init__(workflow, **kwargs)
        self.dropout_ratio = kwargs.get("dropout_ratio")

    def init_unpickled(self):
        super(Dropout, self).init_unpickled()
        self.cl_sources_["dropout.cl"] = {}

    @property
    def dropout_ratio(self):
        """ Gets the relative amount of weights to disable.
        """
        return self._dropout_ratio

    @dropout_ratio.setter
    def dropout_ratio(self, value):
        """ Sets the relative amount of weights to disable.
        """
        assert value is None or 0. < value < 1.
        self._dropout_ratio = value


class DropoutForward(Dropout):
    """
    Forward propagation of dropout layer.
    """
    def __init__(self, workflow, **kwargs):
        self.input = None  # input value of forward layer
        self.mask = formats.Vector()  # dropout mask
        self.states = formats.Vector()
        self.rnd = kwargs.get("rnd", rnd.default)
        super(DropoutForward, self).__init__(workflow, **kwargs)

    @Dropout.dropout_ratio.setter
    def dropout_ratio(self, value):
        Dropout.dropout_ratio.fset(self, value)
        if self.input is not None:
            self.calc_mask()

    @property
    def output(self):
        return self.input

    def initialize(self, device, **kwargs):
        super(DropoutForward, self).initialize(device=device, **kwargs)
        self.mask.v = np.empty_like(self.input.v)
        self.states.v = self.rnd.randint(
            low=0, high=0x100000000,
            size=self.input.v.size * 4).astype(np.uint32)
        self.input.initialize(device)
        self.states.initialize(device)
        self.mask.initialize(device)
        self._threshold_arg_ = np.empty(1, dtype=np.uint64)
        self._pass_arg_ = np.empty(1, dtype=self.input.v.dtype)

        self.build_program(
            {}, "%s/dropout_forward.cl" % config.root.common.cache_dir,
            dtype=self.input.v.dtype)

        self.krn_ = self.get_kernel("dropout_forward")
        self.krn_.set_arg(0, self.input.v_)
        self.krn_.set_arg(3, self.states.v_)
        self.krn_.set_arg(4, self.mask.v_)
        self.krn_.set_arg(5, self.output.v_)

    def calc_mask(self):
        leave_ratio = 1.0 - self.dropout_ratio
        self.mask.v.ravel()[:] = np.random.uniform(low=-self.dropout_ratio,
                                                   high=leave_ratio,
                                                   size=self.input.v.size)[:]
        np.maximum(self.mask.v, 0, self.mask.v)
        np.ceil(self.mask.v, self.mask.v)
        self.mask.v = (self.mask.v.astype(self.input.v.dtype) /
                       leave_ratio)

    def cpu_run(self):
        self.output.map_invalidate()
        self.mask.map_invalidate()
        self.input.map_read()
        self.calc_mask()
        self.output.v = self.input.v * self.mask.v

    def ocl_run(self):
        self.input.unmap()
        self.states.unmap()
        self.mask.unmap()
        self.output.unmap()
        self._threshold_arg_[0] = ((1 << 64) + 0.) * self.dropout_ratio
        self._pass_arg_[0] = 1.0 / (1.0 - self.dropout_ratio)
        self.krn_.set_arg(1, self._threshold_arg_)
        self.krn_.set_arg(2, self._pass_arg_)
        self.execute_kernel(self.krn_, (self.input.v.size,), None).wait()


class DropoutBackward(Dropout):
    """
    Backward propagation of droupout layer.
    """
    def __init__(self, workflow, **kwargs):
        self.mask = None  # dropout mask (should be given from forward unit)
        self.err_y = None  # output error of fwd layer, our input error
        super(DropoutBackward, self).__init__(workflow, **kwargs)

    def initialize(self, device, **kwargs):
        super(DropoutBackward, self).initialize(device=device, **kwargs)
        self.err_y.initialize(device)

        self.build_program(
            {}, "%s/dropout_backward.cl" % (config.root.common.cache_dir),
            dtype=self.err_y.v.dtype)

        self.krn_ = self.get_kernel("dropout_backward")
        self.krn_.set_arg(0, self.mask.v_)
        self.krn_.set_arg(1, self.err_y.v_)

    def cpu_run(self):
        self.err_y.map_read()
        self.mask.map_read()
        np.multiply(self.err_y.v.ravel(), self.mask.v.ravel(),
                    formats.ravel(self.err_y.v))

    def ocl_run(self):
        self.err_y.unmap()
        self.mask.unmap()
        self.execute_kernel(self.krn_, (self.err_y.v.size,), None).wait()
