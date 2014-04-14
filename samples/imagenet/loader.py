"""
Created on Apr 10, 2014

@author: Vadim Markovtsev <v.markovtsev@samsung.com>
"""


from concurrent.futures import ThreadPoolExecutor
import cv2
import jpeg4py
import json
import leveldb
import numpy
from progressbar import ProgressBar
import struct
import os
import xmltodict

import veles.config as config
import veles.opencl_types as opencl_types
from veles.external.prettytable import PrettyTable
import veles.znicz.loader as loader


class Loader(loader.Loader):
    """
    Imagenet images and metadata loader.
    """

    MAPPING = {
        "train": {
            "2013": {
                "img": ("ILSVRC2012_img_train", "ILSVRC2012_bbox_train_v2"),
                "DET": ("ILSVRC2013_DET_train", "ILSVRC2013_DET_bbox_train"),
            },
        },
        "validation": {
            "2013": {
                "img": ("ILSVRC2012_img_val", "ILSVRC2012_bbox_val_v3"),
                "DET": ("ILSVRC2013_DET_val", "ILSVRC2013_DET_bbox_val"),
            },
        },
        "test": {
            "2013": {
                "img": ("ILSVRC2012_img_test", ""),
                "DET": ("ILSVRC2013_DET_test", ""),
            },
        }
    }

    def __init__(self, workflow, **kwargs):
        self._dbpath = kwargs.get("dbpath", config.root.imagenet.dbpath)
        super(Loader, self).__init__(workflow, **kwargs)
        self._ipath = kwargs.get("ipath", config.root.imagenet.ipath)
        self._year = kwargs.get("year", config.root.imagenet.year)
        self._series = kwargs.get("series", config.root.imagenet.series)
        aperture = kwargs.get("aperture",
                              config.get(config.root.imagenet.aperture) or 256)
        self._data_shape = (aperture, aperture)
        self._dtype = opencl_types.dtypes[config.root.common.precision_type]
        self._crop_color = kwargs.get(
            "crop_color",
            config.get(config.root.imagenet.crop_color) or (127, 127, 127))
        self._colorspace = kwargs.get(
            "colorspace", config.get(config.root.imagenet.colorspace) or "RGB")
        self._include_derivative = kwargs.get(
            "derivative", config.get(config.root.imagenet.derivative) or False)
        self._sobel_kernel_size = kwargs.get(
            "sobel_kernel_size",
            config.get(config.root.imagenet.sobel_ksize) or 5)
        self._force_reinit = kwargs.get(
            "force_reinit",
            config.get(config.root.imagenet.force_reinit) or False)

    def init_unpickled(self):
        super(Loader, self).init_unpickled()
        self._db_ = leveldb.LevelDB(self._dbpath)
        self._executor_ = ThreadPoolExecutor(
            config.get(config.root.imagenet.thread_pool_size) or 4)

    @property
    def images_path(self):
        return self._ipath

    @property
    def db_path(self):
        return self._dbpath

    @property
    def year(self):
        return self._year

    @property
    def series(self):
        return self._series

    def load_data(self):
        self._init_files()
        self._init_metadata()
        self._init_labels()
        self._fill_class_samples()

    def create_minibatches(self):
        count = self.minibatch_maxsize
        minibatch_shape = [count] + list(self._data_shape) + \
            [3 + (1 if self._include_derivative else 0)]
        self.minibatch_data << numpy.zeros(shape=minibatch_shape,
                                           dtype=self._dtype)
        self.minibatch_labels << numpy.zeros(count, dtype=numpy.int32)
        self.minibatch_indexes << numpy.zeros(count, dtype=numpy.int32)

    def fill_minibatch(self):
        images = self._executor_.map(
            lambda i: (i, self._get_sample(self.shuffled_indexes[i])),
            range(self.minibatch_size))
        for i, data in images:
            self.minibatch_data[i] = data
        for i in range(self.minibatch_size):
            try:
                meta = self._get_meta(self.shuffled_indexes[i])
                name = meta["object"]["name"]
            except KeyError:
                fn = self._get_file_name(self.shuffled_indexes[i])
                name = os.path.basename(os.path.dirname(fn))
            self.minibatch_labels[i] = self._label_map[name]

    def _get_file_name(self, index):
        for i in range(len(self._files_locator) - 1):
            left_index, files, set_name = self._files_locator[i]
            right_index = self._files_locator[i + 1][0]
            if left_index <= index < right_index:
                mapping = Loader.MAPPING[set_name][self.year][self.series]
                return os.path.join(self._ipath, mapping[0],
                                    files[index - left_index]) + ".JPEG"

    def _decode_image(self, index):
        file_name = self._get_file_name(index)
        try:
            data = jpeg4py.JPEG(file_name).decode()
        except:
            self.exception("Failed to decode %s", file_name)
            raise
        return data

    def _crop_and_scale(self, img, index):
        width = img.shape[1]
        height = img.shape[0]
        try:
            meta = self._get_meta(index)
            bbox_obj = meta["object"]["bndbox"]
            bbox = [int(bbox_obj["xmin"]), int(bbox_obj["ymin"]),
                    int(bbox_obj["xmax"]), int(bbox_obj["ymax"])]
        except (KeyError, ValueError):
            # No bbox found: crop the squared area and resize it
            offset = (width - height) / 2
            if offset > 0:
                img = img[:, offset:(width - offset), :]
            else:
                img = img[offset:(height - offset), :, :]
            cv2.resize(img, self._data_shape, img,
                       interpolation=cv2.INTER_AREA)
            return img
        # Check if the specified bbox is a square
        offset = (bbox[2] - bbox[0] - (bbox[3] - bbox[1])) / 2
        if offset > 0:
            # Width is bigger than height
            bbox[1] -= offset
            bbox[3] += offset
            bottom_height = -bbox[1]
            if bottom_height > 0:
                bbox[1] = 0
            else:
                bottom_height = 0
            top_height = bbox[3] - height
            if top_height > 0:
                bbox[3] = height
            else:
                top_height = 0
            img = img[bbox[1]:bbox[3], bbox[0]:bbox[2], :]
            if bottom_height > 0:
                fixup = numpy.array((bottom_height, bbox[2] - bbox[0], 3))
                fixup.fill(self._crop_color)
                img = numpy.concatenate((fixup, img), axis=0)
            if top_height > 0:
                fixup = numpy.array((top_height, bbox[2] - bbox[0], 3))
                fixup.fill(self._crop_color)
                img = numpy.concatenate((img, fixup), axis=0)
        elif offset < 0:
            # Height is bigger than width
            bbox[0] -= offset
            bbox[2] += offset
            left_width = -bbox[0]
            if left_width > 0:
                bbox[0] = 0
            else:
                left_width = 0
            right_width = bbox[2] - width
            if right_width > 0:
                bbox[2] = width
            else:
                right_width = 0
            img = img[bbox[1]:bbox[3], bbox[0]:bbox[2], :]
            if left_width > 0:
                fixup = numpy.array((bbox[3] - bbox[1], left_width, 3))
                fixup.fill(self._crop_color)
                img = numpy.concatenate((fixup, img), axis=1)
            if right_width > 0:
                fixup = numpy.array((bbox[3] - bbox[1], right_width, 3))
                fixup.fill(self._crop_color)
                img = numpy.concatenate((img, fixup), axis=1)
        else:
            img = img[bbox[1]:bbox[3], bbox[0]:bbox[2], :]
        assert img.shape[0] == img.shape[1]
        if img.shape[0] != self._data_shape[0]:
            img = cv2.resize(img, self._data_shape,
                             interpolation=cv2.INTER_AREA)
        return img

    def _preprocess_sample(self, data):
        if self._include_derivative:
            deriv = cv2.cvtColor(data, cv2.COLOR_RGB2GRAY)
            deriv = cv2.Sobel(deriv,
                              cv2.CV_32F if self._dtype == numpy.float32
                              else cv2.CV_64F,
                              1, 1, ksize=self._sobel_kernel_size)
        if self._colorspace != "RGB":
            cv2.cvtColor(data, getattr(cv2, "COLOR_RGB2" + self._colorspace),
                         data)
        if self._include_derivative:
            shape = list(data.shape)
            shape[-1] += 1
            res = numpy.empty(shape, dtype=self._dtype)
            res[:, :, :-1] = data[:, :, :]
            begindex = len(shape)
            res.ravel()[begindex::(begindex + 1)] = deriv.ravel()
        else:
            res = data.astype(self._dtype)
        return res

    def _get_sample(self, index):
        data = self._decode_image(index)
        data = self._crop_and_scale(data, index)
        data = self._preprocess_sample(data)
        return data

    def _img_file_name(self, base, full):
        res = full[len(os.path.commonprefix([base, full])):]
        res = os.path.splitext(res)[0]
        while (res[0] == os.sep):
            res = res[1:]
        return res

    def _fixup_duplicate_dirs(self, path):
        parts = path.split(os.sep)
        if len(parts) >= 2 and parts[0] == parts[1]:
            res = os.sep.join(parts[1:])
            return res
        return path

    def _init_files(self):
        self.debug("Initializing files table...")
        files_key = ("files_%s_%s" % (self.year, self.series)).encode()
        if not self._force_reinit:
            try:
                files = self._db_.Get(files_key)
                self.info("Loaded files table from DB")
                self._files = json.loads(files.decode())
                do_init = False
            except KeyError:
                self.info("Initializing files table from scratch...")
                do_init = True
        else:
            do_init = True
        if do_init:
            self.debug("Will look for images in %s", self._ipath)
            self._files = {}
            index = 0
            for set_name, years in Loader.MAPPING.items():
                imgs = []
                subdir = years[self.year][self.series][0]
                path = os.path.join(self._ipath, subdir)
                self.info("Scanning %s...", path)
                for root, _, files in os.walk(path, followlinks=True):
                    imgs.extend([self._img_file_name(path,
                                                     os.path.join(root, f))
                                 for f in files
                                 if os.path.splitext(f)[1] == ".JPEG" and
                                 f.find("-256") < 0])
                self._files[set_name] = (imgs, index)
                index += len(imgs)
            self.info("Saving files table to DB...")
            self._db_.Put(files_key, json.dumps(self._files).encode())
            self.info("Initialized files table")
        self._files_locator = sorted([(files[1], files[0], set_name)
                                      for set_name, files
                                      in self._files.items()])
        self._files_locator.append((self._files_locator[-1][0] +
                                    len(self._files_locator[-1][1]),
                                    None, None))

    def _gen_img_key(self, index):
        return struct.pack("I", index) + self.year.encode() + \
            self.series.encode()

    def _get_meta(self, index):
        return json.loads(self._db_.Get(self._gen_img_key(index)).decode())

    def _set_meta(self, index, value):
        self._db_.Put(self._gen_img_key(index), json.dumps(value).encode())

    def _init_metadata(self):
        self.debug("Initializing metadata...")
        metadata_key = ("metadata_%s_%s" % (self.year, self.series)).encode()
        # self._db_.Delete(metadata_key)
        if not self._force_reinit:
            try:
                self._db_.Get(metadata_key)
                self.info("Found metadata in DB")
                return
            except KeyError:
                self.info("Initializing metadata from scratch...")
        self.debug("Will look for metadata in %s", self._ipath)
        all_xmls = {}
        for set_name, years in Loader.MAPPING.items():
            all_xmls[set_name] = xmls = []
            subdir = years[self.year][self.series][1]
            if not subdir:
                continue
            path = os.path.join(self._ipath, subdir)
            self.info("Scanning %s...", path)
            for root, _, files in os.walk(path, followlinks=True):
                xmls.extend([os.path.join(root, f)
                             for f in files
                             if os.path.splitext(f)[1] == ".xml"])
        self.info("Building image indices mapping")
        ifntbl = {}
        for set_name, files in self._files.items():
            flist = files[0]
            base = files[1]
            table = {}
            for i in range(len(flist)):
                table[self._fixup_duplicate_dirs(flist[i])] = i + base
            if len(table) < len(flist):
                self.error("Duplicate file names detected in %s (%s, %s)",
                           set_name, self.year, self.series)
            ifntbl[set_name] = table
        self.info("Parsing XML files...")
        progress = ProgressBar(maxval=sum(
            [len(xmls) for xmls in all_xmls.values()]))
        progress.start()
        for set_name, xmls in all_xmls.items():
            for xml in xmls:
                progress.inc()
                with open(xml, "r") as fr:
                    tree = xmltodict.parse(fr.read())
                del tree["annotation"]["folder"]
                del tree["annotation"]["filename"]
                file_key = self._img_file_name(os.path.join(
                    self._ipath,
                    Loader.MAPPING[set_name][self.year][self.series][1]), xml)
                try:
                    index = ifntbl[set_name][file_key]
                except KeyError:
                    self.error(
                        "%s references unexistent file %s", xml, os.path.join(
                            self._ipath,
                            Loader.MAPPING[set_name][self.year]
                            [self.series][0], file_key))
                    continue
                self._set_meta(index, tree["annotation"])
        progress.finish()
        self._db_.Put(metadata_key, b"")
        self.info("Initialized metadata")

    def _init_labels(self):
        self.debug("Initializing labels...")
        label_key = ("labels_%s_%s" % (self.year, self.series)).encode()
        # self._db_.Delete(label_key)
        if not self._force_reinit:
            try:
                self._label_map = json.loads(self._db_.Get(label_key).decode())
                self.info("Found %d labels in DB", len(self._label_map))
                return
            except KeyError:
                self.info("Initializing labels from scratch...")
        names = set()
        self._metadata_misses = {}
        progress = ProgressBar(maxval=sum(len(f[0]) for f
                                              in self._files.values()))
        progress.start()
        for set_name, files in self._files.items():
            flist = files[0]
            base = files[1]
            self._metadata_misses[set_name] = 0
            for i in range(len(flist)):
                progress.inc()
                try:
                    meta = self._get_meta(base + i)
                    names.add(meta["object"]["name"])
                except:
                    self._metadata_misses[set_name] += 1
                    names.add(os.path.basename(os.path.dirname(flist[i])))
        progress.finish()
        self.info("Sorting labels...")
        label_indices = [n for n in names]
        label_indices.sort()
        self._label_map = {name: i for i, name in enumerate(label_indices)}
        self.info("Saving labels to DB...")
        self._db_.Put(label_key, json.dumps(self._label_map).encode())
        self.info("Initialized %d labels", len(self._label_map))
        table = PrettyTable("set", "files", "bbox", "bbox/files, %")
        table.align["set"] = "l"
        table.align["files"] = "l"
        for set_name, files in self._files.items():
            meta_count = len(files[0]) - self._metadata_misses[set_name]
            table.add_row(set_name, len(files[0]), meta_count,
                          int(meta_count * 100 / len(files[0])))
        self.info("Stats:\n%s", str(table))

    def _fill_class_samples(self):
        for set_name, files in self._files.items():
            index = loader.TRIAGE[set_name]
            self.class_samples[index] = len(files[0])
