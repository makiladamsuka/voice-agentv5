import 'dart:async';
import 'package:flutter/foundation.dart';
import 'package:sound_stream/sound_stream.dart';

class MicVadRecorder {
  final RecorderStream _recorder = RecorderStream();
  StreamSubscription<List<int>>? _audioSubscription;
  bool _isRecording = false;
  double _currentVolume = 0.0;

  // Callback to stream audio bytes (e.g. PCM)
  Function(List<int> chunk)? onAudioChunk;
  Function(double volume)? onVolumeChanged;

  MicVadRecorder();

  bool get isRecording => _isRecording;
  double get currentVolume => _currentVolume;

  Future<void> initialize() async {
    await _recorder.initialize();
  }

  Future<void> start() async {
    if (_isRecording) return;
    try {
      await _recorder.start();
      _isRecording = true;

      _audioSubscription = _recorder.audioStream.listen((data) {
        if (!_isRecording) return;

        // Process chunk volume
        _calculateVolume(data);

        if (onAudioChunk != null) {
          onAudioChunk!(data);
        }
      });
    } catch (e) {
      _isRecording = false;
    }
  }

  void stop() {
    if (!_isRecording) return;
    _recorder.stop();
    _audioSubscription?.cancel();
    _audioSubscription = null;
    _isRecording = false;
    _currentVolume = 0.0;
    if (onVolumeChanged != null) {
      onVolumeChanged!(0.0);
    }
  }

  void _calculateVolume(List<int> samples) {
    if (samples.isEmpty) return;
    double sum = 0;
    // Calculate root-mean-square (RMS) of samples to determine voice amplitude
    for (var sample in samples) {
      sum += sample * sample;
    }
    double rms = sum / samples.length;
    // Scale standard normalized volume (0.0 to 1.0)
    _currentVolume = (rms / 32768.0).clamp(0.0, 1.0);
    if (onVolumeChanged != null) {
      onVolumeChanged!(_currentVolume);
    }
  }
}
