"""
Class for reading data from from Tucker Davis TTank format.
Terminology:
TDT hold data with tanks (actually a directory). And tanks hold sub block
(sub directories).
Tanks correspond to neo.Block and tdt block correspond to neo.Segment.

Note the name Block is ambiguous because it does not refer to same thing in TDT
terminology and neo.


In a directory there are several files:
  * TSQ timestamp index of data
  * TBK some kind of channel info and maybe more
  * TEV contains data : spike + event + signal (for old version)
  * SEV contains signals (for new version)
  * ./sort/ can contain offline spikesorting label for spike
     and can be use place of TEV.

Units in this IO are not guaranteed.

Author: Samuel Garcia, SummitKwan, Chadwick Boulay

"""
from .baserawio import (BaseRawIO, _signal_channel_dtype, _signal_stream_dtype,
                _spike_channel_dtype, _event_channel_dtype)

import numpy as np
import os
import re
import warnings
from collections import OrderedDict
from pathlib import Path


class TdtRawIO(BaseRawIO):
    extensions = ['tbk', 'tdx', 'tev', 'tin', 'tnt', 'tsq', 'sev', 'txt']
    rawmode = 'one-dir'

    def __init__(self, dirname='', sortname=''):
        """
        Initialize reader for one or multiple TDT data blocks.

        dirname (str, pathlib.Path):
            tank-directory of a dataset to be read as multiple segments OR single file of dataset.
            In the latter case only the corresponding segment will considered.
        sortname (str):
            'sortname' is used to specify the external sortcode generated by offline spike sorting.
            if sortname=='PLX', there should be a ./sort/PLX/*.SortResult file in the tdt block,
            which stores the sortcode for every spike
            Default: '', uses the original online sort.


        """
        BaseRawIO.__init__(self)
        dirname = Path(dirname)
        if dirname.is_dir():
            self.dirname = Path(dirname)
            self.tdt_block_mode = 'multi'
        elif dirname.is_file():
            # in single tdt block mode the dirname also contains the block prefix
            self.dirname = dirname.with_suffix('')
            self.tdt_block_mode = 'single'
        else:
            raise ValueError(f'No data folder or file found for {dirname}')

        self.sortname = sortname

    def _source_name(self):
        return self.dirname

    def _get_filestem(self, segment_name=''):
        if self.tdt_block_mode == 'multi':
            return self.dirname / segment_name / f'{self.dirname.name}_{segment_name}'
        else:
            return self.dirname

    def _parse_header(self):
        segment_names = []
        if self.tdt_block_mode == 'multi':
            tankname = self.dirname.stem
            for path in self.dirname.iterdir():
                if is_tdtblock(path):
                    segment_names.append(path.stem)

        # if no block structure was detected, check if current dir contains a set of data
        elif is_tdtblock(self.dirname.parent):
            segment_names.append(str(self.dirname.stem))
            tankname = None

        nb_segment = len(segment_names)
        if nb_segment == 0:
            warnings.warn(f'Could not find any data set belonging to {self.dirname}')

        # TBK (channel info)
        info_channel_groups = None
        for seg_index, segment_name in enumerate(segment_names):

            # TBK contain channels
            tbk_filename = self._get_filestem(segment_name).with_suffix('.Tbk')
            _info_channel_groups = read_tbk(tbk_filename)
            if info_channel_groups is None:
                info_channel_groups = _info_channel_groups
            else:
                assert np.array_equal(info_channel_groups,
                                      _info_channel_groups), 'Channels differ across segments'

        # TEV (mixed data)
        self._tev_datas = []
        for seg_index, segment_name in enumerate(segment_names):
            tev_filename = self._get_filestem(segment_name).with_suffix('.tev')
            if tev_filename.exists():
                tev_data = np.memmap(tev_filename, mode='r', offset=0, dtype='uint8')
            else:
                tev_data = None
            self._tev_datas.append(tev_data)

        # TSQ index with timestamp
        self._tsq = []
        self._seg_t_starts = []
        self._seg_t_stops = []
        for seg_index, segment_name in enumerate(segment_names):
            tsq_filename = self._get_filestem(segment_name).with_suffix('.tsq')
            tsq = np.fromfile(tsq_filename, dtype=tsq_dtype)
            self._tsq.append(tsq)
            # Start and stop times are only found in the second
            #  and last header row, respectively.
            if tsq[1]['evname'] == chr(EVMARK_STARTBLOCK).encode():
                self._seg_t_starts.append(tsq[1]['timestamp'])
            else:
                self._seg_t_starts.append(np.nan)
                print('segment start time not found')
            if tsq[-1]['evname'] == chr(EVMARK_STOPBLOCK).encode():
                self._seg_t_stops.append(tsq[-1]['timestamp'])
            else:
                self._seg_t_stops.append(np.nan)
                print('segment stop time not found')

            # If there exists an external sortcode in ./sort/[sortname]/*.SortResult
            #  (generated after offline sorting)
            if self.sortname != '':
                try:
                    if self.tdt_block_mode == 'multi':
                        path = self.dirname
                    else:
                        path = self.dirname.parent
                    for file in os.listdir(path / 'sort' / self.sortname):
                        if file.endswith(".SortResult"):
                            sortresult_filename = path / 'sort' / self.sortname / file
                            # get new sortcode
                            newsortcode = np.fromfile(sortresult_filename, 'int8')[
                                1024:]  # first 1024 bytes are header
                            # update the sort code with the info from this file
                            tsq['sortcode'][1:-1] = newsortcode
                            # print('sortcode updated')
                            break
                except OSError:
                    pass

        # Re-order segments according to their start times
        sort_inds = np.argsort(self._seg_t_starts)
        if not np.array_equal(sort_inds, list(range(nb_segment))):
            segment_names = [segment_names[x] for x in sort_inds]
            self._tev_datas = [self._tev_datas[x] for x in sort_inds]
            self._seg_t_starts = [self._seg_t_starts[x] for x in sort_inds]
            self._seg_t_stops = [self._seg_t_stops[x] for x in sort_inds]
            self._tsq = [self._tsq[x] for x in sort_inds]
        self._global_t_start = self._seg_t_starts[0]

        # signal channels EVTYPE_STREAM
        signal_streams = []
        signal_channels = []
        self._sigs_data_buf = {seg_index: {} for seg_index in range(nb_segment)}
        self._sigs_index = {seg_index: {} for seg_index in range(nb_segment)}
        self._sig_dtype_by_group = {}  # key = group_id
        self._sig_sample_per_chunk = {}  # key = group_id
        self._sigs_lengths = {seg_index: {}
                              for seg_index in range(nb_segment)}  # key = seg_index then group_id
        self._sigs_t_start = {seg_index: {}
                              for seg_index in range(nb_segment)}  # key = seg_index then group_id

        keep = info_channel_groups['TankEvType'] == EVTYPE_STREAM
        missing_sev_channels = []
        for stream_index, info in enumerate(info_channel_groups[keep]):
            self._sig_sample_per_chunk[stream_index] = info['NumPoints']

            stream_name = str(info['StoreName'])
            stream_id = f'{stream_index}'
            signal_streams.append((stream_name, stream_id))

            for c in range(info['NumChan']):
                global_chan_index = len(signal_channels)
                chan_id = c + 1  # several StoreName can have same chan_id: this is ok

                # loop over segment to get sampling_rate/data_index/data_buffer
                sampling_rate = None
                dtype = None
                for seg_index, segment_name in enumerate(segment_names):
                    # get data index
                    tsq = self._tsq[seg_index]
                    mask = ((tsq['evtype'] == EVTYPE_STREAM) | \
                           (tsq['evtype'] == EVTYPE_STREAM_VARIANT)) & \
                           (tsq['evname'] == info['StoreName']) & \
                           (tsq['channel'] == chan_id)
                    data_index = tsq[mask].copy()
                    self._sigs_index[seg_index][global_chan_index] = data_index

                    size = info['NumPoints'] * data_index.size
                    if stream_index not in self._sigs_lengths[seg_index]:
                        self._sigs_lengths[seg_index][stream_index] = size
                    else:
                        assert self._sigs_lengths[seg_index][stream_index] == size

                    # signal start time, relative to start of segment
                    if len(data_index['timestamp']):
                        t_start = data_index['timestamp'][0]
                    else:
                        # if no signal present use segment t_start as dummy value
                        t_start = self._seg_t_starts[seg_index]
                    if stream_index not in self._sigs_t_start[seg_index]:
                        self._sigs_t_start[seg_index][stream_index] = t_start
                    else:
                        assert self._sigs_t_start[seg_index][stream_index] == t_start

                    # sampling_rate and dtype
                    if len(data_index):
                        _sampling_rate = float(data_index['frequency'][0])
                        _dtype = data_formats[data_index['dataformat'][0]]
                    else:
                        # if no signal present use dummy values
                        _sampling_rate = 1.
                        _dtype = int
                    if sampling_rate is None:
                        sampling_rate = _sampling_rate
                        dtype = _dtype
                        if stream_index not in self._sig_dtype_by_group:
                            self._sig_dtype_by_group[stream_index] = np.dtype(dtype)
                        else:
                            assert self._sig_dtype_by_group[stream_index] == dtype
                    else:
                        assert sampling_rate == _sampling_rate, 'sampling is changing!!!'
                        assert dtype == _dtype, 'sampling is changing!!!'

                    # data buffer test if SEV file exists otherwise TEV
                    # path = self.dirname / segment_name
                    if self.tdt_block_mode == 'multi':
                        # for multi block datasets the names of sev files are fixed
                        store = info['StoreName'].decode('ascii')
                        sev_stem = f'{tankname}_{segment_name}_{store}_ch{chan_id}'
                        sev_filename = (path / sev_stem).with_suffix('.sev')
                    else:
                        # for single block datasets the exact name of sev files in not known
                        sev_regex = f"*_[cC]h{chan_id}.sev"
                        sev_filename = list(self.dirname.parent.glob(str(sev_regex)))
                        # in case multiple sev files are found, try to find the one for current stream
                        if len(sev_filename) > 1:
                            store = info['StoreName'].decode('ascii')
                            sev_regex = f'*_{store}_Ch{chan_id}.sev'
                            sev_filename = list(self.dirname.parent.glob(str(sev_regex)))

                        # in case non or multiple sev files are found for current stream + channel
                        if len(sev_filename) != 1:
                            missing_sev_channels.append(chan_id)
                            sev_filename = None
                        else:
                            sev_filename = sev_filename[0]
                            
                    if (sev_filename is not None) and sev_filename.exists():
                        data = np.memmap(sev_filename, mode='r', offset=0, dtype='uint8')
                    else:
                        data = self._tev_datas[seg_index]
                    assert data is not None, 'no TEV nor SEV'
                    self._sigs_data_buf[seg_index][global_chan_index] = data

                chan_name = '{} {}'.format(info['StoreName'], c + 1)
                sampling_rate = sampling_rate
                units = 'V'  # WARNING this is not sur at all
                gain = 1.
                offset = 0.
                signal_channels.append((chan_name, str(chan_id), sampling_rate, dtype,
                                        units, gain, offset, stream_id))

        if missing_sev_channels:
            warnings.warn(f'Could not identify sev files for channels {missing_sev_channels}.')

        signal_streams = np.array(signal_streams, dtype=_signal_stream_dtype)
        signal_channels = np.array(signal_channels, dtype=_signal_channel_dtype)

        # unit channels EVTYPE_SNIP
        self.internal_unit_ids = {}
        self._waveforms_size = []
        self._waveforms_dtype = []
        spike_channels = []
        keep = info_channel_groups['TankEvType'] == EVTYPE_SNIP
        tsq = np.hstack(self._tsq)
        # If there is no chance the different TSQ files will have different units,
        #  then we can do tsq = self._tsq[0]
        for info in info_channel_groups[keep]:
            for c in range(info['NumChan']):
                chan_id = c + 1
                mask = (tsq['evtype'] == EVTYPE_SNIP) & \
                       (tsq['evname'] == info['StoreName']) & \
                       (tsq['channel'] == chan_id)
                unit_ids = np.unique(tsq[mask]['sortcode'])
                for unit_id in unit_ids:
                    unit_index = len(spike_channels)
                    self.internal_unit_ids[unit_index] = (info['StoreName'], chan_id, unit_id)

                    unit_name = "ch{}#{}".format(chan_id, unit_id)
                    wf_units = 'V'
                    wf_gain = 1.
                    wf_offset = 0.
                    wf_left_sweep = info['NumPoints'] // 2
                    wf_sampling_rate = info['SampleFreq']
                    spike_channels.append((unit_name, '{}'.format(unit_id),
                                          wf_units, wf_gain, wf_offset,
                                          wf_left_sweep, wf_sampling_rate))

                    self._waveforms_size.append(info['NumPoints'])
                    self._waveforms_dtype.append(np.dtype(data_formats[info['DataFormat']]))

        spike_channels = np.array(spike_channels, dtype=_spike_channel_dtype)

        # signal channels EVTYPE_STRON
        event_channels = []
        keep = info_channel_groups['TankEvType'] == EVTYPE_STRON
        for info in info_channel_groups[keep]:
            chan_name = info['StoreName']
            chan_id = 1
            event_channels.append((chan_name, chan_id, 'event'))

        event_channels = np.array(event_channels, dtype=_event_channel_dtype)

        # fill into header dict
        self.header = {}
        self.header['nb_block'] = 1
        self.header['nb_segment'] = [nb_segment]
        self.header['signal_streams'] = signal_streams
        self.header['signal_channels'] = signal_channels
        self.header['spike_channels'] = spike_channels
        self.header['event_channels'] = event_channels

        # Annotations only standard ones:
        self._generate_minimal_annotations()

    def _block_count(self):
        return 1

    def _segment_count(self, block_index):
        return self.header['nb_segment'][block_index]

    def _segment_t_start(self, block_index, seg_index):
        return self._seg_t_starts[seg_index] - self._global_t_start

    def _segment_t_stop(self, block_index, seg_index):
        return self._seg_t_stops[seg_index] - self._global_t_start

    def _get_signal_size(self, block_index, seg_index, stream_index):
        size = self._sigs_lengths[seg_index][stream_index]
        return size

    def _get_signal_t_start(self, block_index, seg_index, stream_index):
        return self._sigs_t_start[seg_index][stream_index] - self._global_t_start

    def _get_analogsignal_chunk(self, block_index, seg_index, i_start, i_stop,
                                stream_index, channel_indexes):
        if i_start is None:
            i_start = 0
        if i_stop is None:
            i_stop = self._sigs_lengths[seg_index][stream_index]

        stream_id = self.header['signal_streams'][stream_index]['id']
        signal_channels = self.header['signal_channels']
        mask = signal_channels['stream_id'] == stream_id
        global_chan_indexes = np.arange(signal_channels.size)[mask]
        signal_channels = signal_channels[mask]

        if channel_indexes is None:
            channel_indexes = slice(None)
        global_chan_indexes = global_chan_indexes[channel_indexes]
        signal_channels = signal_channels[channel_indexes]

        dt = self._sig_dtype_by_group[stream_index]
        raw_signals = np.zeros((i_stop - i_start, signal_channels.size), dtype=dt)

        sample_per_chunk = self._sig_sample_per_chunk[stream_index]
        bl0 = i_start // sample_per_chunk
        bl1 = int(np.ceil(i_stop / sample_per_chunk))
        chunk_nb_bytes = sample_per_chunk * dt.itemsize

        for c, global_index in enumerate(global_chan_indexes):
            data_index = self._sigs_index[seg_index][global_index]
            data_buf = self._sigs_data_buf[seg_index][global_index]

            # loop over data blocks and get chunks
            ind = 0
            for bl in range(bl0, bl1):
                ind0 = data_index[bl]['offset']
                ind1 = ind0 + chunk_nb_bytes
                data = data_buf[ind0:ind1].view(dt)

                if bl == bl1 - 1:
                    # right border
                    # be careful that bl could be both bl0 and bl1!!
                    border = data.size - (i_stop % sample_per_chunk)
                    # cut data if not all samples are requested
                    if border != len(data):
                        data = data[:-border]
                if bl == bl0:
                    # left border
                    border = i_start % sample_per_chunk
                    data = data[border:]

                raw_signals[ind:data.size + ind, c] = data
                ind += data.size

        return raw_signals

    def _get_mask(self, tsq, seg_index, evtype, evname, chan_id, unit_id, t_start, t_stop):
        """Used inside spike and events methods"""
        mask = (tsq['evtype'] == evtype) & \
               (tsq['evname'] == evname) & \
               (tsq['channel'] == chan_id)

        if unit_id is not None:
            mask &= (tsq['sortcode'] == unit_id)

        if t_start is not None:
            mask &= tsq['timestamp'] >= (t_start + self._global_t_start)

        if t_stop is not None:
            mask &= tsq['timestamp'] <= (t_stop + self._global_t_start)

        return mask

    def _spike_count(self, block_index, seg_index, unit_index):
        store_name, chan_id, unit_id = self.internal_unit_ids[unit_index]
        tsq = self._tsq[seg_index]
        mask = self._get_mask(tsq, seg_index, EVTYPE_SNIP, store_name,
                              chan_id, unit_id, None, None)
        nb_spike = np.sum(mask)
        return nb_spike

    def _get_spike_timestamps(self, block_index, seg_index, unit_index, t_start, t_stop):
        store_name, chan_id, unit_id = self.internal_unit_ids[unit_index]
        tsq = self._tsq[seg_index]
        mask = self._get_mask(tsq, seg_index, EVTYPE_SNIP, store_name,
                              chan_id, unit_id, t_start, t_stop)
        timestamps = tsq[mask]['timestamp']
        timestamps -= self._global_t_start
        return timestamps

    def _rescale_spike_timestamp(self, spike_timestamps, dtype):
        # already in s
        spike_times = spike_timestamps.astype(dtype)
        return spike_times

    def _get_spike_raw_waveforms(self, block_index, seg_index, unit_index, t_start, t_stop):
        store_name, chan_id, unit_id = self.internal_unit_ids[unit_index]
        tsq = self._tsq[seg_index]
        mask = self._get_mask(tsq, seg_index, EVTYPE_SNIP, store_name,
                              chan_id, unit_id, t_start, t_stop)
        nb_spike = np.sum(mask)

        data = self._tev_datas[seg_index]

        dt = self._waveforms_dtype[unit_index]
        nb_sample = self._waveforms_size[unit_index]
        waveforms = np.zeros((nb_spike, 1, nb_sample), dtype=dt)

        for i, e in enumerate(tsq[mask]):
            ind0 = e['offset']
            ind1 = ind0 + nb_sample * dt.itemsize
            waveforms[i, 0, :] = data[ind0:ind1].view(dt)

        return waveforms

    def _event_count(self, block_index, seg_index, event_channel_index):
        h = self.header['event_channels'][event_channel_index]
        store_name = h['name'].encode('ascii')
        tsq = self._tsq[seg_index]
        chan_id = 0
        mask = self._get_mask(tsq, seg_index, EVTYPE_STRON, store_name, chan_id, None, None, None)
        nb_event = np.sum(mask)
        return nb_event

    def _get_event_timestamps(self, block_index, seg_index, event_channel_index, t_start, t_stop):
        h = self.header['event_channels'][event_channel_index]
        store_name = h['name'].encode('ascii')
        tsq = self._tsq[seg_index]
        chan_id = 0
        mask = self._get_mask(tsq, seg_index, EVTYPE_STRON, store_name, chan_id, None, None, None)

        timestamps = tsq[mask]['timestamp']
        timestamps -= self._global_t_start
        labels = tsq[mask]['offset'].astype('U')
        durations = None
        # TODO if user demand event to epoch
        # with EVTYPE_STROFF=258
        # and so durations would be not None
        # it was not implemented in previous IO.
        return timestamps, durations, labels

    def _rescale_event_timestamp(self, event_timestamps, dtype, event_channel_index):
        # already in s
        ev_times = event_timestamps.astype(dtype)
        return ev_times


tbk_field_types = [
    ('StoreName', 'S4'),
    ('HeadName', 'S16'),
    ('Enabled', 'bool'),
    ('CircType', 'int'),
    ('NumChan', 'int'),
    ('StrobeMode', 'int'),
    ('TankEvType', 'int32'),
    ('NumPoints', 'int'),
    ('DataFormat', 'int'),
    ('SampleFreq', 'float64'),
]


def read_tbk(tbk_filename):
    """
    Tbk contains some visible header in txt mode to describe
    channel group info.
    """
    with open(tbk_filename, mode='rb') as f:
        txt_header = f.read()

    infos = []
    for chan_grp_header in txt_header.split(b'[STOREHDRITEM]'):
        if chan_grp_header.startswith(b'[USERNOTEDELIMITER]'):
            break

        # parse into a dict
        info = OrderedDict()
        # name and type have to be words, but value can contain floating numbers
        pattern = br'NAME=(\w+);TYPE=(\w+);VALUE=(\S+);'
        r = re.findall(pattern, chan_grp_header)
        for name, _type, value in r:
            info[name.decode('ascii')] = value
        infos.append(info)

    # and put into numpy
    info_channel_groups = np.zeros(len(infos), dtype=tbk_field_types)
    for i, info in enumerate(infos):
        for k, dt in tbk_field_types:
            v = np.dtype(dt).type(info[k])
            info_channel_groups[i][k] = v

    return info_channel_groups


tsq_dtype = [
    ('size', 'int32'),  # bytes 0-4
    ('evtype', 'int32'),  # bytes 5-8
    ('evname', 'S4'),  # bytes 9-12
    ('channel', 'uint16'),  # bytes 13-14
    ('sortcode', 'uint16'),  # bytes 15-16
    ('timestamp', 'float64'),  # bytes 17-24
    ('offset', 'int64'),  # bytes 25-32
    ('dataformat', 'int32'),  # bytes 33-36
    ('frequency', 'float32'),  # bytes 37-40
]

EVTYPE_UNKNOWN = int('00000000', 16)  # 0
EVTYPE_STRON = int('00000101', 16)  # 257
EVTYPE_STROFF = int('00000102', 16)  # 258
EVTYPE_SCALAR = int('00000201', 16)  # 513
EVTYPE_STREAM = int('00008101', 16)  # 33025
EVTYPE_STREAM_VARIANT = int('00008111', 16)  # 33041
EVTYPE_SNIP = int('00008201', 16)  # 33281
EVTYPE_MARK = int('00008801', 16)  # 34817
EVTYPE_HASDATA = int('00008000', 16)  # 32768
EVTYPE_UCF = int('00000010', 16)  # 16
EVTYPE_PHANTOM = int('00000020', 16)  # 32
EVTYPE_MASK = int('0000FF0F', 16)  # 65295
EVTYPE_INVALID_MASK = int('FFFF0000', 16)  # 4294901760
EVMARK_STARTBLOCK = int('0001', 16)  # 1
EVMARK_STOPBLOCK = int('0002', 16)  # 2

data_formats = {
    0: 'float32',
    1: 'int32',
    2: 'int16',
    3: 'int8',
    4: 'float64',
}


def is_tdtblock(blockpath):
    """Is the path a TDT block (=neo.Segment) ?"""
    file_ext = list()
    if blockpath.is_dir():
        # for every file, get extension, convert to lowercase and append
        for file in blockpath.iterdir():
            file_ext.append(file.suffix.lower())

    file_ext = set(file_ext)
    tdt_ext = {'.tbk', '.tdx', '.tev', '.tsq'}
    if file_ext >= tdt_ext:  # if containing all the necessary files
        return True
    else:
        return False
