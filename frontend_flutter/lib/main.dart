import 'package:flutter/material.dart';
import 'package:audioplayers/audioplayers.dart';
import 'services/nlu_websocket_client.dart';
import 'services/mic_vad_recorder.dart';
import 'views/welcome_view.dart';
import 'views/session_view.dart';

void main() {
  runApp(const KioskApp());
}

class KioskApp extends StatelessWidget {
  const KioskApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'NEma Voice Kiosk',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        brightness: Brightness.dark,
        useMaterial3: true,
      ),
      home: const KioskHomePage(),
    );
  }
}

class KioskHomePage extends StatefulWidget {
  const KioskHomePage({super.key});

  @override
  State<KioskHomePage> createState() => _KioskHomePageState();
}

class _KioskHomePageState extends State<KioskHomePage> {
  late NluWebSocketClient _nluClient;
  late MicVadRecorder _micRecorder;
  final AudioPlayer _audioPlayer = AudioPlayer();

  bool _isSessionActive = false;
  double _micVolume = 0.0;
  final List<Map<String, String>> _chatHistory = [];

  @override
  void initState() {
    super.initState();
    // Initialize NLU WebSocket client (points to localhost backend)
    _nluClient = NluWebSocketClient(serverUrl: "ws://localhost:8765/ws/voice");

    // Initialize VAD mic recorder
    _micRecorder = MicVadRecorder();
    _micRecorder.initialize();

    // Hook listeners
    _nluClient.onStateChanged = (state) {
      if (state == 'speaking') {
        // If speaking, temporarily mute mic capture to avoid feedback loops
        _micRecorder.stop();
      } else if (state == 'listening' && _isSessionActive) {
        _micRecorder.start();
      }
    };

    _nluClient.onResponseReceived = (text, audioUrl, action) {
      setState(() {
        _chatHistory.add({'sender': 'agent', 'text': text});
      });

      if (audioUrl != null) {
        // Build resolved backend host URL
        final String resolvedAudio = "http://localhost:8080$audioUrl";
        _playServerAudio(resolvedAudio);
      } else {
        // Send fallback complete to backend immediately
        _nluClient.sendTtsDone();
      }
    };

    _micRecorder.onVolumeChanged = (volume) {
      setState(() {
        _micVolume = volume;
      });
    };

    // Auto-connect to backend NLU
    _nluClient.connect();
  }

  Future<void> _playServerAudio(String url) async {
    try {
      await _audioPlayer.play(UrlSource(url));
      _audioPlayer.onPlayerComplete.first.then((_) {
        // Notify NLU backend speaking is finished, so it resets the VAD loop
        _nluClient.sendTtsDone();
        if (_isSessionActive) {
          _micRecorder.start();
        }
      });
    } catch (e) {
      _nluClient.sendTtsDone();
    }
  }

  void _startVoiceSession() {
    setState(() {
      _isSessionActive = true;
      _chatHistory.clear();
    });
    _micRecorder.start();
  }

  void _stopVoiceSession() {
    _micRecorder.stop();
    setState(() {
      _isSessionActive = false;
      _chatHistory.clear();
    });
  }

  @override
  void dispose() {
    _nluClient.disconnect();
    _micRecorder.stop();
    _audioPlayer.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: AnimatedSwitcher(
          duration: const Duration(milliseconds: 400),
          child: _isSessionActive
              ? SessionView(
                  voiceState: _nluClient.state,
                  micVolume: _micVolume,
                  lastTranscript: _nluClient.lastTranscript,
                  chatHistory: _chatHistory,
                  lastAction: _nluClient.lastAction,
                  onTapStop: _stopVoiceSession,
                )
              : WelcomeView(
                  onTapStart: _startVoiceSession,
                ),
        ),
      ),
    );
  }
}
