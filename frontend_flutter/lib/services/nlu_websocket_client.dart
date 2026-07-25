import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

enum NluVoiceState { idle, listening, thinking, speaking }

class NluWebSocketClient extends ChangeNotifier {
  final String serverUrl;
  WebSocketChannel? _channel;
  bool _isConnected = false;
  NluVoiceState _state = NluVoiceState.idle;
  String _lastTranscript = "";
  Map<String, dynamic>? _lastAction;

  // Callbacks for UI interaction
  Function(String text, String? audioUrl, Map<String, dynamic>? action)? onResponseReceived;
  Function(String state)? onStateChanged;

  NluWebSocketClient({required this.serverUrl});

  bool get isConnected => _isConnected;
  NluVoiceState get state => _state;
  String get lastTranscript => _lastTranscript;
  Map<String, dynamic>? get lastAction => _lastAction;

  void connect() {
    if (_isConnected) return;
    try {
      _channel = WebSocketChannel.connect(Uri.parse(serverUrl));
      _isConnected = true;
      notifyListeners();

      _channel!.stream.listen(
        (message) {
          _handleMessage(message);
        },
        onDone: () {
          _handleDisconnect();
        },
        onError: (error) {
          _handleDisconnect();
        },
      );
    } catch (e) {
      _handleDisconnect();
    }
  }

  void _handleMessage(dynamic rawMessage) {
    try {
      final data = jsonDecode(rawMessage as String) as Map<String, dynamic>;
      final type = data['type'] as String?;

      if (type == 'pong') {
        // Keep-alive response
        return;
      }

      if (type == 'state') {
        final convState = data['conv_state'] as String?;
        if (convState != null) {
          _updateState(convState);
        }
      } else if (type == 'response') {
        final text = data['text'] as String? ?? "";
        final audioUrl = data['audio'] as String?;
        final action = data['action'] as Map<String, dynamic>?;
        final utteranceId = data['utterance_id'] as String?;

        if (action != null && action.isNotEmpty) {
          _lastAction = action;
        }

        if (onResponseReceived != null) {
          onResponseReceived!(text, audioUrl, action);
        }
      }
    } catch (e) {
      // JSON decoding or handle error
    }
  }

  void _updateState(String convState) {
    switch (convState) {
      case 'listening':
        _state = NluVoiceState.listening;
        break;
      case 'thinking':
        _state = NluVoiceState.thinking;
        break;
      case 'speaking':
        _state = NluVoiceState.speaking;
        break;
      default:
        _state = NluVoiceState.idle;
    }
    notifyListeners();
    if (onStateChanged != null) {
      onStateChanged!(convState);
    }
  }

  void sendTranscript(String text) {
    if (!_isConnected || _channel == null) return;
    _channel!.sink.add(jsonEncode({
      'type': 'transcript',
      'text': text,
    }));
    _state = NluVoiceState.thinking;
    notifyListeners();
  }

  void sendPlaybackStart(String utteranceId) {
    if (!_isConnected || _channel == null) return;
    _channel!.sink.add(jsonEncode({
      'type': 'playback_start',
      'utterance_id': utteranceId,
    }));
  }

  void sendTtsDone() {
    if (!_isConnected || _channel == null) return;
    _channel!.sink.add(jsonEncode({
      'type': 'tts_done',
    }));
  }

  void sendUserSpeaking() {
    if (!_isConnected || _channel == null) return;
    _channel!.sink.add(jsonEncode({
      'type': 'user_speaking',
    }));
  }

  void disconnect() {
    _channel?.sink.close();
    _handleDisconnect();
  }

  void _handleDisconnect() {
    _isConnected = false;
    _state = NluVoiceState.idle;
    _channel = null;
    notifyListeners();
    // Auto-reconnect after 3 seconds
    Future.delayed(const Duration(seconds: 3), () {
      if (!_isConnected) connect();
    });
  }
}
