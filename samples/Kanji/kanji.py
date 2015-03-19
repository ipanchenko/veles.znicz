#!/usr/bin/python3 -O
# encoding: utf-8
"""
Created on June 29, 2013


Model created for Chinese characters recognition. Dataset was generated by
VELES with generate_kanji.py utility.
Model – fully-connected Neural Network with MSE loss function.

Copyright (c) 2013 Samsung Electronics Co., Ltd.
"""


import logging

from veles.config import root
from veles.znicz.standard_workflow import StandardWorkflow


class KanjiWorkflow(StandardWorkflow):
    """
    Model created for Chinese characters recognition. Dataset was generated by
    VELES with generate_kanji.py utility.
    Model – fully-connected Neural Network with MSE loss function.
    """
    def create_workflow(self):
        self.link_repeater(self.start_point)
        self.link_loader(self.repeater)
        self.link_forwards(("input", "minibatch_data"), self.loader)
        self.link_evaluator(self.forwards[-1])
        self.link_decision(self.evaluator)
        end_units = [link(self.decision) for link in (self.link_snapshotter,
                                                      self.link_image_saver)]
        if root.kanji.add_plotters:
            end_units.extend((
                self.link_error_plotter(self.decision),
                self.link_weights_plotter(
                    root.kanji.weights_plotter.limit,
                    "weights", self.decision),
                self.link_min_max_plotter(False, self.decision),
                self.link_min_max_plotter(True, self.max_plotter[-1]),
                self.link_mse_plotter(self.decision)))

        self.link_loop(self.link_gds(*end_units))
        self.link_end_point(*end_units)

    def initialize(self, device, weights, bias, **kwargs):
        super(KanjiWorkflow, self).initialize(device=device, **kwargs)
        if weights is not None:
            for i, fwds in enumerate(self.forwards):
                fwds.weights.map_invalidate()
                fwds.weights.mem[:] = weights[i][:]
        if bias is not None:
            for i, fwds in enumerate(self.forwards):
                fwds.bias.map_invalidate()
                fwds.bias.mem[:] = bias[i][:]


def run(load, main):
    weights = None
    bias = None
    w, snapshot = load(
        KanjiWorkflow,
        decision_config=root.kanji.decision,
        loader_config=root.kanji.loader,
        loader_name=root.kanji.loader_name,
        snapshotter_config=root.kanji.snapshotter,
        layers=root.kanji.layers,
        image_saver_config=root.kanji.image_saver,
        loss_function=root.kanji.loss_function)
    if snapshot:
        if type(w) == tuple:
            logging.info("Will load weights")
            weights = w[0]
            bias = w[1]
        else:
            logging.info("Will load workflow")
            logging.info("Weights and bias ranges per layer are:")
            for fwds in w.fwds:
                logging.info("%f %f %f %f" % (
                    fwds.weights.mem.min(), fwds.weights.mem.max(),
                    fwds.bias.mem.min(), fwds.bias.mem.max()))
            w.decision.improved <<= True
    main(weights=weights, bias=bias)
