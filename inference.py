from __future__ import absolute_import, division, print_function, unicode_literals

import glob
import os
import argparse
import json
import torch
from scipy.io.wavfile import write
from env import AttrDict
from meldataset import mel_spectrogram, MAX_WAV_VALUE, load_wav
from models import Generator
import random
from scipy.signal import resample
import time
from torchaudio import transforms

h = None
device = None


def load_checkpoint(filepath, device):
    assert os.path.isfile(filepath)
    print("Loading '{}'".format(filepath))
    checkpoint_dict = torch.load(filepath, map_location=device)
    print("Complete.")
    return checkpoint_dict


def get_mel(x):
    return mel_spectrogram(x, h.n_fft, h.num_mels, h.sampling_rate, h.hop_size, h.win_size, h.fmin, h.fmax)


def scan_checkpoint(cp_dir, prefix):
    pattern = os.path.join(cp_dir, prefix + '*')
    cp_list = glob.glob(pattern)
    if len(cp_list) == 0:
        return ''
    return sorted(cp_list)[-1]


def inference(a):
    mel_spec = transforms.MelSpectrogram(sample_rate=h.sampling_rate,
                                         n_fft=h.n_fft,
                                         win_length=h.win_size,
                                         pad=int((h.n_fft - h.hop_size) / 2),
                                         pad_mode="reflect",
                                         hop_length=h.hop_size,
                                         f_min=h.fmin,
                                         f_max=h.fmax,
                                         n_mels=h.num_mels,
                                         window_fn=torch.hann_window,
                                         power=2,
                                         normalized=False,
                                         center=False,
                                         onesided=True).to(device)
    generator = Generator(h).to(device)

    state_dict_g = load_checkpoint(a.checkpoint_file, device)
    generator.load_state_dict(state_dict_g['generator'])

    filelist = os.listdir(a.input_wavs_dir)

    os.makedirs(a.output_dir, exist_ok=True)

    generator.eval()
    generator.remove_weight_norm()
    with torch.no_grad():
        for i, filname in enumerate(filelist):
            wav, sr = load_wav(os.path.join(a.input_wavs_dir, filname))
            wav = wav / MAX_WAV_VALUE

            if sr != h.sampling_rate:
                number_of_samples = round(len(wav) * float(h.sampling_rate) / sr)
                wav = resample(wav, number_of_samples)

            wav = torch.FloatTensor(wav).to(device)
            wav = wav.unsqueeze(0)

            if wav.size(1) >= h.segment_size:
                max_audio_start = wav.size(1) - h.segment_size
                audio_start = random.randint(0, max_audio_start)
                wav = wav[:, audio_start:audio_start + h.segment_size]

            # x = get_mel(wav)
            x = mel_spec(wav)
            current = time.time()
            y_g_hat = generator(x)
            print(time.time() - current)
            audio = y_g_hat.squeeze()
            audio = audio * MAX_WAV_VALUE
            audio = audio.cpu().numpy().astype('int16')

            output_file = os.path.join(a.output_dir, os.path.splitext(filname)[0] + '_generated.wav')
            write(output_file, h.sampling_rate, audio)
            print(output_file)


def main():
    print('Initializing Inference Process..')

    parser = argparse.ArgumentParser()
    parser.add_argument('--input_wavs_dir', default='test_files')
    parser.add_argument('--output_dir', default='generated_files')
    parser.add_argument('--checkpoint_file', required=True)
    a = parser.parse_args()

    config_file = os.path.join(os.path.split(a.checkpoint_file)[0], 'config.json')
    with open(config_file) as f:
        data = f.read()

    global h
    json_config = json.loads(data)
    h = AttrDict(json_config)

    torch.manual_seed(h.seed)
    global device
    if torch.cuda.is_available():
        torch.cuda.manual_seed(h.seed)
        device = torch.device('cuda')
    else:
        device = torch.device('cpu')

    inference(a)


if __name__ == '__main__':
    main()

