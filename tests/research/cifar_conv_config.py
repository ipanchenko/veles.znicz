#!/usr/bin/python3.3 -O
"""
Created on Mart 21, 2014

Example of Mnist config.

Copyright (c) 2013 Samsung Electronics Co., Ltd.
"""


import os

from veles.config import root


# optional parameters

train_dir = os.path.join(root.common.test_dataset_root, "cifar/10")
validation_dir = os.path.join(root.common.test_dataset_root,
                              "cifar/10/test_batch")

root.update = {"decision": {"fail_iterations": 100, "do_export_weights": True},
               "snapshotter": {"prefix": "cifar"},
               "loader": {"minibatch_size": 100},
               "image_saver": {"out_dirs":
                               [os.path.join(root.common.cache_dir,
                                             "tmp/test"),
                                os.path.join(root.common.cache_dir,
                                             "tmp/validation"),
                                os.path.join(root.common.cache_dir,
                                             "tmp/train")]},
               "weights_plotter": {"limit": 64},
               "cifar": {"layers":
                         [{"type": "conv", "n_kernels": 64,
                           "kx": 5, "ky": 5, "padding": (2, 2, 2, 2),
                           "learning_rate": 0.0001,
                           "weights_decay": 0.004},
                          {"type": "activation_log"},
                          {"type": "avg_pooling",
                           "kx": 3, "ky": 3, "sliding": (2, 2)},

                          {"type": "conv", "n_kernels": 64,
                           "kx": 5, "ky": 5, "padding": (2, 2, 2, 2),
                           "learning_rate": 0.0001,
                           "weights_decay": 0.004},
                          {"type": "activation_log"},

                          {"type": "conv", "n_kernels": 64,
                           "kx": 5, "ky": 5, "padding": (2, 2, 2, 2),
                           "learning_rate": 0.0001,
                           "weights_decay": 0.004},
                          {"type": "activation_log"},

                          {"type": "softmax", "output_shape": 10,
                           "learning_rate": 0.0001,
                           "weights_decay": 0.004}],
                         "data_paths": {"train": train_dir,
                                        "validation": validation_dir}}}
