from collections import defaultdict
import datetime
import fnmatch
from glob import glob
import h5py
import numpy as np
import os.path as osp
import pandas as pd
import re
import xarray

__all__ = ['DataCollection', 'RunDirectory', 'H5File',
           'SourceNameError', 'PropertyNameError',
           'stack_data', 'stack_detector_data', 'by_index', 'by_id',
           ]

from .reader import (SourceNameError, PropertyNameError,
                     stack_data, stack_detector_data,
                     by_id, by_index, FilenameInfo
                    )


class DataCollection:
    def __init__(self):
        self.instrument_sources = set()
        self.control_sources = set()
        self.train_ids = []

        # {source: [(train_id_range, file)]}
        self._source_file_mapping = {}
        # {(file, source, group): (firsts, counts)}
        self._index_cache = {}
        # {source: set(keys)}
        self._source_keys = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    @property
    def all_sources(self):
        return self.instrument_sources | self.control_sources

    @property
    def files(self):
        res = set()
        for data in self._source_file_mapping.values():
            for _, file in data:
                res.add(file)
        return res

    def add_file(self, path):
        f = h5py.File(path)

        tid_data = f['INDEX/trainId'].value
        train_ids = tid_data[tid_data != 0]

        sources = set()

        for source in f['METADATA/dataSourceId'].value:
            if not source:
                continue
            source = source.decode()
            category, _, h5_source = source.partition('/')
            if category == 'INSTRUMENT':
                device, _, chan_grp = h5_source.partition(':')
                chan, _, group = chan_grp.partition('/')
                source = device + ':' + chan
                self.instrument_sources.add(source)
                sources.add(source)
                # TODO: Do something with group
            elif category == 'CONTROL':
                self.control_sources.add(h5_source)
                sources.add(h5_source)
            else:
                raise ValueError("Unknown data category %r" % category)

        for source in sources:
            if source not in self._source_file_mapping:
                self._source_file_mapping[source] = []
            self._source_file_mapping[source].append((train_ids, f))

        self.train_ids = sorted(set(self.train_ids).union(train_ids))

    def _expand_selection(self, selection):
        if isinstance(selection, set):
            return selection

        res = set()
        if isinstance(selection, dict):
            # {source: {key1, key2}}
            # {source: {}} -> all keys for this source
            for source, keys in selection.items():  #
                if source not in self.all_sources:
                    raise ValueError("Source {} not in this run".format(source))

                for k in (keys or ['*']):
                    res.add((source, k))

        elif isinstance(selection, list):
            # [('src_glob', 'key_glob'), ...]
            for src_glob, key_glob in selection:
                matched = self._select_glob(src_glob, key_glob)

                if not matched:
                    raise ValueError("No matches for pattern {}"
                                     .format((src_glob, key_glob)))

                res.update(matched)
        else:
            TypeError("Unknown selection type: {}".format(type(selection)))

        return res

    def _select_glob(self, source_glob, key_glob):
        source_re = re.compile(fnmatch.translate(source_glob))
        key_re = re.compile(fnmatch.translate(key_glob))
        if key_glob.endswith(('.value', '*')):
            ctrl_key_re = key_re
        else:
            # The translated pattern ends with "\Z" - insert before this
            p = key_re.pattern
            end_ix = p.rindex('\Z')
            ctrl_key_re = re.compile(p[:end_ix] + r'(\.value)?' + p[end_ix:])

        matched = set()
        for source in self.all_sources:
            if not source_re.match(source):
                continue

            if key_glob == '*':
                matched.add((source, '*'))
            else:
                r = ctrl_key_re if source in self.control_sources else key_re
                for key in filter(r.match, self._keys_for_source(source)):
                    matched.add((source, key))

        if not matched:
            raise ValueError("No matches for pattern {}"
                             .format((source_glob, key_glob)))
        return matched

    def select(self, seln_or_source_glob, key_glob='*'):
        """Return a new DataCollection with selected sources & keys
        """
        if isinstance(seln_or_source_glob, str):
            selection = self._select_glob(seln_or_source_glob, key_glob)
        else:
            selection = self._expand_selection(seln_or_source_glob)

        selection = self._expand_selection(selection)
        selected_sources = {s for (s, k) in selection}

        res = DataCollection()
        res.instrument_sources = self.instrument_sources & selected_sources
        res.control_sources = self.control_sources & selected_sources
        res._source_file_mapping = {s: self._source_file_mapping[s]
                                    for s in selected_sources}
        res._index_cache = {(f, s, g): v for ((f, s, g), v) in self._index_cache.items()
                            if s in selected_sources}
        collect_train_ids = set()
        for data in res._source_file_mapping.values():
            for train_ids, _ in data:
                collect_train_ids.update(train_ids)
        res.train_ids = sorted(collect_train_ids)

        selected_keys = defaultdict(set)
        for source, key in selection:
            selected_keys[source].add(key)

        for source, keys in selected_keys.items():
            if '*' in keys:
                if source in self._source_keys:
                    res._source_keys[source] = self._source_keys[source]
            else:
                res._source_keys[source] = keys

        return res

    def select_trains(self, train_range):
        if isinstance(train_range, by_id):
            start_ix = _tid_to_slice_ix(train_range.value.start, self.train_ids, stop=False)
            stop_ix = _tid_to_slice_ix(train_range.value.stop, self.train_ids, stop=True)
            ix_slice = slice(start_ix, stop_ix, train_range.value.step)
        elif isinstance(train_range, by_index):
            ix_slice = train_range.value
        else:
            raise TypeError(type(train_range))

        res = DataCollection()
        res.train_ids = self.train_ids[ix_slice]
        sfm = defaultdict(list)
        for source, data in self._source_file_mapping.items():
            for train_ids, file in data:
                if np.intersect1d(train_ids, res.train_ids).size > 0:
                    sfm[source].append((train_ids, file))
        res._source_file_mapping = dict(sfm)
        res._source_keys = {s: k for (s, k) in self._source_keys
                            if s in res._source_file_mapping}
        res.control_sources = {s for s in self.control_sources
                               if s in res._source_file_mapping}
        res.instrument_sources = {s for s in self.instrument_sources
                                  if s in res._source_file_mapping}
        res._index_cache = {(f, s, g): v for ((f, s, g), v) in self._index_cache.items()
                            if s in res._source_file_mapping}
        return res

    def _check_field(self, source, key):
        if source not in self.all_sources:
            raise SourceNameError(source)
        if key not in self._keys_for_source(source):
            raise PropertyNameError(key, source)

    def get_array(self, source, key, extra_dims=None):
        self._check_field(source, key)
        seq_arrays = []

        if source in self.control_sources:
            data_path = "/CONTROL/{}/{}".format(source, key.replace('.', '/'))
            for trainids, f in self._source_file_mapping[source]:
                data = f[data_path][:len(trainids), ...]
                if extra_dims is None:
                    extra_dims = ['dim_%d' % i for i in range(data.ndim - 1)]
                dims = ['trainId'] + extra_dims

                seq_arrays.append(
                    xarray.DataArray(data, dims=dims, coords={'trainId': trainids}))

        elif source in self.instrument_sources:
            data_path = "/INSTRUMENT/{}/{}".format(source, key.replace('.', '/'))
            for trainids, f in self._source_file_mapping[source]:
                group = key.partition('.')[0]
                firsts, counts = self._get_index(f, source, group)
                if (counts > 1).any():
                    raise ValueError("{}/{} data has more than one data point per train"
                                     .format(source, group))
                trainids = self._expand_trainids(firsts, counts, trainids)

                data = f[data_path][:len(trainids), ...]

                if extra_dims is None:
                    extra_dims = ['dim_%d' % i for i in range(data.ndim - 1)]
                dims = ['trainId'] + extra_dims

                seq_arrays.append(
                    xarray.DataArray(data, dims=dims, coords={'trainId': trainids}))
        else:
            raise SourceNameError(source)

        non_empty = [a for a in seq_arrays if (a.size > 0)]
        if not non_empty:
            if seq_arrays:
                # All per-file arrays are empty, so just return the first one.
                return seq_arrays[0]

            raise Exception(("Unable to get data for source {!r}, key {!r}. "
                             "Please report an issue so we can investigate")
                            .format(source, key))

        return xarray.concat(sorted(non_empty,
                                    key=lambda a: a.coords['trainId'][0]),
                             dim='trainId')

    def get_series(self, source, key):
        """Return a pandas Series for a particular data field.

        Parameters
        ----------

        source: str
            Device name with optional output channel, e.g.
            "SA1_XTD2_XGM/DOOCS/MAIN" or "SPB_DET_AGIPD1M-1/DET/7CH0:xtdf"
        key: str
            Key of parameter within that device, e.g. "beamPosition.iyPos.value"
            or "header.linkId". The data must be 1D in the file.
        """
        self._check_field(source, key)
        name = source + '/' + key
        if name.endswith('.value'):
            name = name[:-6]

        seq_series = []

        if source in self.control_sources:
            data_path = "/CONTROL/{}/{}".format(source, key.replace('.', '/'))
            for trainids, f in self._source_file_mapping[source]:
                data = f[data_path][:len(trainids), ...]
                index = pd.Index(trainids, name='trainId')

                seq_series.append(pd.Series(data, name=name, index=index))

        elif source in self.instrument_sources:
            data_path = "/INSTRUMENT/{}/{}".format(source, key.replace('.', '/'))
            for trainids, f in self._source_file_mapping[source]:
                group = key.partition('.')[0]
                firsts, counts = self._get_index(f, source, group)
                trainids = self._expand_trainids(firsts, counts, trainids)

                index = pd.Index(trainids, name='trainId')
                data = f[data_path][:]
                if not index.is_unique:
                    pulse_id = f['/INSTRUMENT/{}/{}/pulseId'
                                 .format(source, group)]
                    pulse_id = pulse_id[:len(index), 0]
                    index = pd.MultiIndex.from_arrays([trainids, pulse_id],
                                                      names=['trainId', 'pulseId'])
                    # Does pulse-oriented data always have an extra dimension?
                    assert data.shape[1] == 1
                    data = data[:, 0]
                data = data[:len(index)]

                seq_series.append(pd.Series(data, name=name, index=index))
        else:
            raise Exception("Unknown source category")

        return pd.concat(sorted(seq_series, key=lambda s: s.index[0]))

    def get_dataframe(self, fields=None, *, timestamps=False):
        if fields is not None:
            return self.select(fields).get_dataframe(timestamps=timestamps)

        series = []
        for source in self.all_sources:
            for key in self._keys_for_source(source):
                if (not timestamps) and key.endswith('.timestamp'):
                    continue
                series.append(self.get_series(source, key))

        return pd.concat(series, axis=1)

    def _get_index(self, file, source, group):
        """Get first index & count for a source and for a specific train ID.

        Indices are cached; this appears to provide some performance benefit.
        """
        try:
            return self._index_cache[(file, source, group)]
        except KeyError:
            ix = self._read_index(file, source, group)
            self._index_cache[(file, source, group)] = ix
            return ix

    def _read_index(self, file, source, group):
        """Get first index & count for a source.

        This is 'real' reading when the requested index is not in the cache.
        """
        ix_group = file['/INDEX/{}/{}'.format(source, group)]
        firsts = ix_group['first'][:]
        if 'count' in ix_group:
            counts = ix_group['count'][:]
        else:
            status = ix_group['status'][:]
            counts = np.uint64((ix_group['last'][:] - firsts + 1) * status)
        return firsts, counts

    def _expand_trainids(self, first, counts, trainIds):
        n = min(len(counts), len(trainIds))
        return np.repeat(trainIds[:n], counts.astype(np.intp)[:n])

    def _keys_for_source(self, source):
        if source not in self.all_sources:
            raise SourceNameError(source)

        try:
            return self._source_keys[source]
        except KeyError:
            pass

        if source in self.control_sources:
            group = '/CONTROL/' + source
        else:
            group = '/INSTRUMENT/' + source

        # The same source may be in multiple files, but this assumes it has
        # the same keys in all files that it appears in.
        for trainids, f in self._source_file_mapping[source]:
            res = set()

            def add_key(key, value):
                if isinstance(value, h5py.Dataset):
                    res.add(key.replace('/', '.'))

            f[group].visititems(add_key)
            self._source_keys[source] = res
            return res

    def _find_data(self, source, train_id):
        for trainids, f in self._source_file_mapping[source]:
            ixs = (trainids == train_id).nonzero()[0]
            if ixs.size > 0:
                return f, ixs[0]

        return None, None

    def train_from_id(self, train_id, devices=None):
        if devices is not None:
            return self.select(devices).train_from_id(train_id)

        res = {}
        for source in self.control_sources:
            source_data = res[source] = {}
            file, pos = self._find_data(source, train_id)
            if file is None:
                continue

            for key in self._keys_for_source(source):
                path = '/CONTROL/{}/{}'.format(source, key.replace('.', '/'))
                source_data[key] = file[path][pos]

        for source in self.instrument_sources:
            source_data = res[source] = {}
            file, pos = self._find_data(source, train_id)
            if file is None:
                continue

            for key in self._keys_for_source(source):
                group = key.partition('.')[0]
                firsts, counts = self._get_index(file, source, group)
                first, count = firsts[pos], counts[pos]
                if not count:
                    continue

                path = '/INSTRUMENT/{}/{}'.format(source, key.replace('.', '/'))
                if count == 1:
                    source_data[key] = file[path][first]
                else:
                    source_data[key] = file[path][first:first+count]

        return train_id, res

    def train_from_index(self, train_index, devices=None):
        train_id = self.train_ids[train_index]
        return self.train_from_id(train_id, devices=devices)

    def _check_data_missing(self, tid) -> bool:
        """Return True if a train does not have data for all sources"""
        for source in self.control_sources:
            file, _ = self._find_data(source, tid)
            if file is None:
                return True

        for source in self.instrument_sources:
            file, pos = self._find_data(source, tid)
            if file is None:
                return True

            groups = {k.partition('.')[0] for k in self._keys_for_source(source)}
            for group in groups:
                _, counts = self._get_index(file, source, group)
                if counts[pos] == 0:
                    return True

        return False

    def info(self):
        """Show information about the run.
        """
        # time info
        first_train = self.train_ids[0]
        last_train = self.train_ids[-1]
        train_count = len(self.train_ids)
        span_sec = (last_train - first_train) / 10
        span_txt = str(datetime.timedelta(seconds=span_sec))

        detector_srcs, non_detector_inst_srcs = [], []
        detector_modules = {}
        for source in self.instrument_sources:
            m = re.match(r'(.+)/DET/(\d+)CH', source)
            if m:
                detector_srcs.append(source)
                name, modno = m.groups((1, 2))
                detector_modules[(name, modno)] = source
            else:
                non_detector_inst_srcs.append(source)

        # A run should only have one detector, but if that changes, don't hide it
        detector_name = ','.join(sorted(set(k[0] for k in detector_modules)))

        # disp
        print('# of trains:   ', train_count)
        print('Duration:      ', span_txt)
        print('First train ID:', first_train)
        print('Last train ID: ', last_train)
        print()

        print("{} detector modules ({})".format(
            len(detector_srcs), detector_name
        ))
        if len(detector_modules) > 0:
            # Show detail on the first module (the others should be similar)
            mod_key = sorted(detector_modules)[0]
            mod_source = detector_modules[mod_key]
            dinfo = self.detector_info(mod_source)
            module = ' '.join(mod_key)
            dims = ' x '.join(str(d) for d in dinfo['dims'])
            print("  e.g. module {} : {} pixels".format(module, dims))
            print("  {} frames per train, {} total frames".format(
                dinfo['frames_per_train'], dinfo['total_frames']
            ))
        print()

        print(len(non_detector_inst_srcs), 'instrument sources (excluding detectors):')
        for d in sorted(non_detector_inst_srcs):
            print('  -', d)
        print()
        print(len(self.control_sources), 'control sources:')
        for d in sorted(self.control_sources):
            print('  -', d)
        print()

    def detector_info(self, source):
        """Get statistics about the detector data.

        Returns a dictionary with keys:
        - 'dims' (pixel dimensions)
        - 'frames_per_train'
        - 'total_frames'
        """
        all_counts = []
        for _, file in self._source_file_mapping[source]:
            _, counts = self._get_index(file, source, 'image')
            all_counts.append(counts)

        all_counts = np.concatenate(all_counts)
        dims = file['/INSTRUMENT/{}/image/data'.format(source)].shape[-2:]

        return {
            'dims': dims,
            # Some trains have 0 frames; max is the interesting value
            'frames_per_train': all_counts.max(),
            'total_frames': all_counts.sum(),
        }

    def trains(self, devices=None, train_range=None, *, require_all=False):
        dc = self
        if devices is not None:
            dc = dc.select(devices)
        if train_range is not None:
            dc = dc.select_trains(train_range)
        return iter(TrainIterator(dc, require_all=require_all))


class TrainIterator:
    def __init__(self, data, require_all=True):
        self.data = data
        self.require_all = require_all
        # {(source, key): (trainids, f, dataset)}
        self._datasets_cache = {}

    def _find_data(self, source, key, tid):
        try:
            cache_tids, file, ds = self._datasets_cache[(source, key)]
        except KeyError:
            pass
        else:
            ixs = (cache_tids == tid).nonzero()[0]
            if ixs.size > 0:
                return file, ixs[0], ds

        data = self.data
        section = 'CONTROL' if source in data.control_sources else 'INSTRUMENT'
        path = '/{}/{}/{}'.format(section, source, key.replace('.', '/'))
        for trainids, f in self.data._source_file_mapping[source]:
            if tid in trainids:
                ds = f[path]
                self._datasets_cache[(source, key)] = (trainids, f, ds)
                ix = (trainids == tid).nonzero()[0][0]
                return f, ix, ds

        return None, None, None

    def _assemble_data(self, tid):
        res = {}
        for source in self.data.control_sources:
            source_data = res[source] = {}
            for key in self.data._keys_for_source(source):
                _, pos, ds = self._find_data(source, key, tid)
                if ds is None:
                    continue
                source_data[key] = ds[pos]

        for source in self.data.instrument_sources:
            source_data = res[source] = {}
            for key in self.data._keys_for_source(source):
                file, pos, ds = self._find_data(source, key, tid)
                if ds is None:
                    continue
                group = key.partition('.')[0]
                firsts, counts = self.data._get_index(file, source, group)
                first, count = firsts[pos], counts[pos]
                if count == 1:
                    source_data[key] = ds[first]
                else:
                    source_data[key] = ds[first:first+count]

        return res

    def __iter__(self):
        print(self.data.train_ids)
        for tid in self.data.train_ids:
            if self.require_all and self.data._check_data_missing(tid):
                print("Skipped", tid)
                continue
            yield tid, self._assemble_data(tid)

def H5File(path):
    d = DataCollection()
    d.add_file(path)
    return d

def RunDirectory(path):
    d = DataCollection()
    for file in filter(h5py.is_hdf5, glob(osp.join(path, '*.h5'))):
        d.add_file(file)
    return d


def _tid_to_slice_ix(tid, train_ids, stop=False):
    """Convert a train ID to an integer index for slicing the dataset

    Throws ValueError if the slice won't overlap the trains in the data.
    The *stop* parameter tells it which end of the slice it is making.
    """
    if tid is None:
        return None

    try:
        return train_ids.index(tid)
    except ValueError:
        pass

    if tid < train_ids[0]:
        if stop:
            raise ValueError("Train ID {} is before this run (starts at {})"
                             .format(tid, train_ids[0]))
        else:
            return None
    elif tid > train_ids[-1]:
        if stop:
            return None
        else:
            raise ValueError("Train ID {} is after this run (ends at {})"
                             .format(tid, train_ids[-1]))
    else:
        # This train ID is within the run, but doesn't have an entry.
        # Find the first ID in the run greater than the one given.
        return (train_ids > tid).nonzero()[0][0]