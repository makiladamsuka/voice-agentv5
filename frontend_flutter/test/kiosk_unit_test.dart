import 'package:flutter_test/flutter_test.dart';
import 'package:frontend_flutter/services/nlu_websocket_client.dart';
import 'package:frontend_flutter/services/mic_vad_recorder.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  group('Kiosk Voice Client Unit Tests', () {
    test('NluWebSocketClient initial state is idle', () {
      final client = NluWebSocketClient(serverUrl: "ws://localhost:8765/ws/voice");
      expect(client.isConnected, isFalse);
      expect(client.state, NluVoiceState.idle);
      expect(client.lastTranscript, isEmpty);
      expect(client.lastAction, isNull);
    });

    test('MicVadRecorder initial state is not recording', () {
      final recorder = MicVadRecorder();
      expect(recorder.isRecording, isFalse);
      expect(recorder.currentVolume, equals(0.0));
    });
  });
}
