import math
import os
import random
import torch
import torch.utils.data
import numpy as np
from librosa.util import normalize
from scipy.io.wavfile import read
from scipy.signal import resample
from librosa.filters import mel as librosa_mel_fn
from collections.abc import Mapping
from torchaudio import transforms

MAX_WAV_VALUE = 32768.0


def load_wav(full_path):
    sampling_rate, data = read(full_path)
    return data, sampling_rate


def dynamic_range_compression(x, C=1, clip_val=1e-5):
    return np.log(np.clip(x, a_min=clip_val, a_max=None) * C)


def dynamic_range_decompression(x, C=1):
    return np.exp(x) / C


def dynamic_range_compression_torch(x, C=1, clip_val=1e-5):
    return torch.log(torch.clamp(x, min=clip_val) * C)


def dynamic_range_decompression_torch(x, C=1):
    return torch.exp(x) / C


def spectral_normalize_torch(magnitudes):
    output = dynamic_range_compression_torch(magnitudes)
    return output


def spectral_de_normalize_torch(magnitudes):
    output = dynamic_range_decompression_torch(magnitudes)
    return output


mel_basis = {}
hann_window = {}


def mel_spectrogram(y, n_fft, num_mels, sampling_rate, hop_size, win_size, fmin, fmax, center=False):
    if torch.min(y) < -1.:
        print('min value is ', torch.min(y))
    if torch.max(y) > 1.:
        print('max value is ', torch.max(y))

    global mel_basis, hann_window
    if fmax not in mel_basis:
        mel = librosa_mel_fn(sr=sampling_rate, n_fft=n_fft, n_mels=num_mels, fmin=fmin, fmax=fmax)
        mel_basis[str(fmax)+'_'+str(y.device)] = torch.from_numpy(mel).float().to(y.device)
        hann_window[str(y.device)] = torch.hann_window(win_size).to(y.device)

    y = torch.nn.functional.pad(y.unsqueeze(1), (int((n_fft-hop_size)/2), int((n_fft-hop_size)/2)), mode='reflect')
    y = y.squeeze(1)

    spec = torch.stft(y, n_fft, hop_length=hop_size, win_length=win_size, window=hann_window[str(y.device)],
                      center=center, pad_mode='reflect', normalized=False, onesided=True, return_complex=True)
    spec = torch.view_as_real(spec)
    spec = torch.sqrt(spec.pow(2).sum(-1)+(1e-9))
    spec = torch.matmul(mel_basis[str(fmax)+'_'+str(y.device)], spec)
    spec = spectral_normalize_torch(spec)
    return spec


def recursive_file_extract(base_pth, cls_pth, cls_queue=[]):
    i = 0
    filename_list = []
    for key in cls_pth:
        if isinstance(cls_pth[key], Mapping):
            cls_queue.append(i)
            flist, llist = recursive_file_extract(base_pth + key + '/', cls_pth[key], cls_queue)
            filename_list = filename_list + flist
            cls_queue = []
        else:
            j = 0
            for child in cls_pth[key]:
                fd = base_pth + key + '/' + child + '/'
                if os.path.isdir(fd):
                    filenames = [os.path.join(fd, f) for f in os.listdir(fd) if os.path.isfile(os.path.join(fd, f))]
                    filename_list = filename_list + filenames
                j = j + 1
        i = i + 1
    return filename_list


# Previously uses argparser arguments.
# Modified to use the paths written in the config files
def get_dataset_filelist(h):
    # with open(a.input_training_file, 'r', encoding='utf-8') as fi:
    #     training_files = [os.path.join(a.input_wavs_dir, x.split('|')[0] + '.wav')
    #                       for x in fi.read().split('\n') if len(x) > 0]
    #
    # with open(a.input_validation_file, 'r', encoding='utf-8') as fi:
    #     validation_files = [os.path.join(a.input_wavs_dir, x.split('|')[0] + '.wav')
    #                         for x in fi.read().split('\n') if len(x) > 0]
    filelist = None
    for base in h.base_pth:
        files = recursive_file_extract(base, h.cls_pth)
        if filelist is None:
            filelist = files
        else:
            filelist = filelist + files
    random.shuffle(filelist)
    training_files = filelist[:int(len(filelist)*(1 - h.num_validation))]
    validation_files = filelist[int(len(filelist) * (1 - h.num_validation)):]

    return training_files, validation_files


class MelDataset(torch.utils.data.Dataset):
    def __init__(self, training_files, segment_size, n_fft, num_mels,
                 hop_size, win_size, sampling_rate,  fmin, fmax, split=True, shuffle=True, n_cache_reuse=1,
                 device=None, fmax_loss=None, fine_tuning=False, base_mels_path=None):
        self.audio_files = training_files
        random.seed(1234)
        if shuffle:
            random.shuffle(self.audio_files)
        self.segment_size = segment_size
        self.sampling_rate = sampling_rate
        self.split = split
        self.n_fft = n_fft
        self.num_mels = num_mels
        self.hop_size = hop_size
        self.win_size = win_size
        self.fmin = fmin
        self.fmax = fmax
        self.fmax_loss = fmax_loss
        self.cached_wav = None
        self.n_cache_reuse = n_cache_reuse
        self._cache_ref_count = 0
        self.device = device
        self.fine_tuning = fine_tuning
        self.base_mels_path = base_mels_path
        self.mel_spec = transforms.MelSpectrogram(sample_rate=sampling_rate,
                                                  n_fft=n_fft,
                                                  pad=int((n_fft-hop_size)/2),
                                                  pad_mode="reflect",
                                                  win_length=win_size,
                                                  hop_length=hop_size,
                                                  f_min=fmin,
                                                  f_max=fmax,
                                                  n_mels=num_mels,
                                                  window_fn=torch.hann_window,
                                                  power=2,
                                                  normalized=False,
                                                  center=False,
                                                  onesided=True)

    def __getitem__(self, index):
        filename = self.audio_files[index]
        if self._cache_ref_count == 0:
            audio, sampling_rate = load_wav(filename)
            audio = audio / MAX_WAV_VALUE
            # if not self.fine_tuning:
            #     audio = normalize(audio) * 0.95
            self.cached_wav = audio
            if sampling_rate != self.sampling_rate:
                number_of_samples = round(len(audio) * float(self.sampling_rate) / sampling_rate)
                audio = resample(audio, number_of_samples)  # Make sure all have the sample rate.
                # raise ValueError("{} SR doesn't match target {} SR".format(
                #     sampling_rate, self.sampling_rate))
            self._cache_ref_count = self.n_cache_reuse
        else:
            audio = self.cached_wav
            self._cache_ref_count -= 1

        audio = torch.FloatTensor(audio)
        audio = audio.unsqueeze(0)

        if not self.fine_tuning:
            if self.split:
                if audio.size(1) >= self.segment_size:
                    max_audio_start = audio.size(1) - self.segment_size
                    audio_start = random.randint(0, max_audio_start)
                    audio = audio[:, audio_start:audio_start+self.segment_size]
                else:
                    audio = torch.nn.functional.pad(audio, (0, self.segment_size - audio.size(1)), 'constant')

            # mel = mel_spectrogram(audio, self.n_fft, self.num_mels,
            #                       self.sampling_rate, self.hop_size, self.win_size, self.fmin, self.fmax,
            #                       center=False)
            mel = self.mel_spec(audio)
        else:
            mel = np.load(
                os.path.join(self.base_mels_path, os.path.splitext(os.path.split(filename)[-1])[0] + '.npy'))
            mel = torch.from_numpy(mel)

            if len(mel.shape) < 3:
                mel = mel.unsqueeze(0)

            if self.split:
                frames_per_seg = math.ceil(self.segment_size / self.hop_size)

                if audio.size(1) >= self.segment_size:
                    mel_start = random.randint(0, mel.size(2) - frames_per_seg - 1)
                    mel = mel[:, :, mel_start:mel_start + frames_per_seg]
                    audio = audio[:, mel_start * self.hop_size:(mel_start + frames_per_seg) * self.hop_size]
                else:
                    mel = torch.nn.functional.pad(mel, (0, frames_per_seg - mel.size(2)), 'constant')
                    audio = torch.nn.functional.pad(audio, (0, self.segment_size - audio.size(1)), 'constant')

        # mel_loss = mel_spectrogram(audio, self.n_fft, self.num_mels,
        #                            self.sampling_rate, self.hop_size, self.win_size, self.fmin, self.fmax_loss,
        #                            center=False)

        mel_loss = self.mel_spec(audio)

        return (mel.squeeze(), audio.squeeze(0), filename, mel_loss.squeeze())

    def __len__(self):
        return len(self.audio_files)
