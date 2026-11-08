import { AudioModule, RecordingPresets, setAudioModeAsync, useAudioRecorder, useAudioRecorderState } from 'expo-audio';
import * as DocumentPicker from 'expo-document-picker';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ActivityIndicator,
  Image,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { ReportCard } from '@/components/report-card';
import {
  connectEvents,
  downloadAttachment,
  getEvents,
  getStatus,
  loadPairing,
  pair,
  savePairing,
  sendMessage,
  settleApproval,
  uploadUri,
} from '@/lib/vanta-api';
import type { ConnectionStatus, GatewayStatus, Pairing, VantaEvent } from '@/types';

type FeedItem = VantaEvent & { key: string };

const DEFAULT_GATEWAY = process.env.EXPO_PUBLIC_VANTA_API_URL || '';
const STATE_EVENTS = new Set(['idle', 'listening', 'thinking', 'speaking', 'working']);
const REPORT_EVENTS = new Set(['show_result', 'rich_result', 'dashboard_document', 'artifacts']);

function payloadText(payload: unknown): string {
  if (typeof payload === 'string') return payload;
  if (!payload) return '';
  if (typeof payload === 'object') {
    const record = payload as Record<string, unknown>;
    for (const key of ['markdown', 'content', 'text', 'message', 'summary', 'detail', 'body']) {
      if (typeof record[key] === 'string') return record[key] as string;
    }
  }
  try { return JSON.stringify(payload, null, 2); } catch { return String(payload); }
}

function eventTitle(item: FeedItem) {
  if (item.kind === 'user_query') return 'YOU';
  if (item.kind === 'approval_request') return 'APPROVAL';
  if (REPORT_EVENTS.has(item.kind)) return 'REPORT';
  if (item.kind === 'error') return 'ISSUE';
  return 'VANTA';
}

function isVisibleEvent(event: VantaEvent) {
  return ['agent_response', 'user_query', 'info', 'error', 'approval_request', 'attachment',
    ...REPORT_EVENTS].includes(event.kind);
}

function ApprovalCard({ item, pairing }: { item: FeedItem; pairing: Pairing }) {
  const data = typeof item.payload === 'object' && item.payload ? item.payload as Record<string, unknown> : {};
  const approvalId = String(data.id || data.approval_id || '');
  const [settled, setSettled] = useState<'approve' | 'reject' | null>(null);
  const [busy, setBusy] = useState(false);

  const decide = async (decision: 'approve' | 'reject') => {
    if (!approvalId || busy) return;
    setBusy(true);
    try {
      await settleApproval(pairing, approvalId, decision);
      setSettled(decision);
    } finally { setBusy(false); }
  };

  return (
    <View style={styles.approvalCard}>
      <Text style={styles.eyebrow}>APPROVAL REQUIRED</Text>
      <Text style={styles.approvalText}>{payloadText(item.payload)}</Text>
      {settled ? (
        <Text style={styles.settled}>{settled === 'approve' ? 'Approved' : 'Declined'}</Text>
      ) : (
        <View style={styles.approvalActions}>
          <Pressable disabled={busy} onPress={() => decide('reject')} style={styles.secondaryButton}>
            <Text style={styles.secondaryButtonText}>Decline</Text>
          </Pressable>
          <Pressable disabled={busy} onPress={() => decide('approve')} style={styles.primaryButton}>
            <Text style={styles.primaryButtonText}>{busy ? 'Sending…' : 'Approve'}</Text>
          </Pressable>
        </View>
      )}
    </View>
  );
}

function FeedCard({ item, pairing }: { item: FeedItem; pairing: Pairing }) {
  if (item.kind === 'approval_request') return <ApprovalCard item={item} pairing={pairing} />;
  if (item.kind === 'attachment') {
    const data = item.payload as Record<string, unknown>;
    return (
      <View style={styles.feedCard}>
        <Text style={styles.eyebrow}>FILE</Text>
        <Text style={styles.feedText}>{String(data.title || data.name || 'VANTA file')}</Text>
        <Text style={styles.subtle}>{String(data.name || '')}</Text>
        <Pressable
          style={[styles.primaryButton, { alignSelf: 'flex-start', marginTop: 12 }]}
          onPress={() => downloadAttachment(pairing, String(data.url || ''), String(data.name || 'vanta-file'))}
        >
          <Text style={styles.primaryButtonText}>Download</Text>
        </Pressable>
      </View>
    );
  }
  const user = item.kind === 'user_query';
  const report = REPORT_EVENTS.has(item.kind);
  const text = payloadText(item.payload);
  if (!text) return null;
  return (
    <View style={[styles.feedCard, user && styles.userCard, report && styles.reportShell]}>
      <Text style={[styles.eyebrow, item.kind === 'error' && styles.errorLabel]}>{eventTitle(item)}</Text>
      {report ? <ReportCard content={text} /> : <Text style={styles.feedText}>{text}</Text>}
      {item.at ? <Text style={styles.timestamp}>{new Date(item.at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</Text> : null}
    </View>
  );
}

function PairScreen({ onPaired }: { onPaired: (value: Pairing) => void }) {
  const [baseUrl, setBaseUrl] = useState(DEFAULT_GATEWAY);
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const connect = async () => {
    if (!baseUrl.trim() || !password) return;
    setBusy(true); setError('');
    try { onPaired(await pair(baseUrl, password)); }
    catch (caught) { setError(caught instanceof Error ? caught.message : 'Could not pair.'); }
    finally { setBusy(false); setPassword(''); }
  };

  return (
    <SafeAreaView style={styles.pairPage}>
      <View style={styles.pairCard}>
        <Image source={require('../../assets/images/vanta-icon.png')} style={styles.logoLarge} />
        <Text style={styles.pairTitle}>Connect to VANTA</Text>
        <Text style={styles.pairCopy}>Pair this phone directly with your VANTA computer. Your password is verified locally and is never saved on the phone.</Text>
        <Text style={styles.inputLabel}>Secure gateway address</Text>
        <TextInput
          autoCapitalize="none" autoCorrect={false} keyboardType="url"
          onChangeText={setBaseUrl} value={baseUrl} placeholder="https://your-laptop.your-tailnet.ts.net"
          placeholderTextColor="#646a76" style={styles.pairInput}
        />
        <Text style={styles.inputLabel}>AUTH_PASSWORD</Text>
        <TextInput
          onChangeText={setPassword} value={password} secureTextEntry
          onSubmitEditing={connect} placeholder="Enter once to pair"
          placeholderTextColor="#646a76" style={styles.pairInput}
        />
        {error ? <Text style={styles.errorText}>{error}</Text> : null}
        <Pressable onPress={connect} disabled={busy || !password} style={[styles.connectButton, (!password || busy) && styles.disabled]}>
          {busy ? <ActivityIndicator color="#07111f" /> : <Text style={styles.connectText}>Pair securely</Text>}
        </Pressable>
        <Text style={styles.pairFootnote}>VANTA must be running. The gateway should be exposed through private HTTPS, not a public open port.</Text>
      </View>
    </SafeAreaView>
  );
}

export default function HomeScreen() {
  const [pairing, setPairing] = useState<Pairing | null>(() => loadPairing());
  const [connection, setConnection] = useState<ConnectionStatus>(() => loadPairing() ? 'connecting' : 'offline');
  const [status, setStatus] = useState<GatewayStatus | null>(null);
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const [activity, setActivity] = useState('Ready');
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [autoSpeak, setAutoSpeak] = useState(true);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const seen = useRef(new Set<string>());
  const scroll = useRef<any>(null);
  const recorder = useAudioRecorder(RecordingPresets.HIGH_QUALITY);
  const recorderState = useAudioRecorderState(recorder);

  const addEvent = useCallback((event: VantaEvent) => {
    const key = event.id ? `server-${event.id}` : `${event.kind}-${event.at || Date.now()}-${JSON.stringify(event.payload).slice(0, 80)}`;
    if (seen.current.has(key)) return;
    seen.current.add(key);
    if (STATE_EVENTS.has(event.kind)) setActivity(event.kind === 'idle' ? 'Ready' : event.kind[0].toUpperCase() + event.kind.slice(1));
    if (event.kind === 'mode_changed') {
      const data = event.payload as Record<string, unknown>;
      setStatus((previous) => previous ? { ...previous, mode: String(data?.mode || data?.label || previous.mode) } : previous);
    }
    if (event.kind === 'speech' && autoSpeak && Platform.OS === 'web' && typeof window !== 'undefined') {
      const text = payloadText(event.payload);
      if (text && 'speechSynthesis' in window) {
        window.speechSynthesis.cancel();
        window.speechSynthesis.speak(new SpeechSynthesisUtterance(text));
      }
    }
    if (event.kind === 'voice_transcript') setDraft(payloadText(event.payload));
    if (event.kind === 'error') setActivity('Needs attention');
    if (isVisibleEvent(event)) setFeed((current) => {
      const withoutOptimisticEcho = event.kind === 'user_query'
        ? current.filter((item) => !(item.key.startsWith('local-') && payloadText(item.payload) === payloadText(event.payload)))
        : current;
      return [...withoutOptimisticEcho.slice(-149), { ...event, key }];
    });
  }, [autoSpeak]);

  useEffect(() => {
    if (Platform.OS === 'web' && 'serviceWorker' in navigator) {
      navigator.serviceWorker.register('/sw.js').catch(() => undefined);
    }
  }, []);

  useEffect(() => {
    if (!pairing) return;
    let active = true;
    getEvents(pairing).then(({ events }) => events.forEach(addEvent)).catch(() => undefined);
    const refreshStatus = () => getStatus(pairing).then((value) => { if (active) setStatus(value); }).catch(() => undefined);
    refreshStatus();
    const timer = setInterval(refreshStatus, 30_000);
    const disconnectSocket = connectEvents(pairing, addEvent, (online) => {
      if (active) setConnection(online ? 'online' : 'connecting');
    });
    return () => { active = false; clearInterval(timer); disconnectSocket(); };
  }, [pairing, addEvent]);

  useEffect(() => { setTimeout(() => scroll.current?.scrollToEnd({ animated: true }), 80); }, [feed.length]);

  const submit = async () => {
    const text = draft.trim();
    if (!pairing || !text || sending) return;
    setSending(true); setDraft('');
    const optimistic: FeedItem = { kind: 'user_query', payload: text, at: new Date().toISOString(), key: `local-${Date.now()}` };
    setFeed((items) => [...items, optimistic]);
    try { await sendMessage(pairing, text); }
    catch (caught) {
      addEvent({ kind: 'error', payload: caught instanceof Error ? caught.message : 'Message failed.', at: new Date().toISOString() });
      setDraft(text);
    } finally { setSending(false); }
  };

  const pickFile = async () => {
    if (!pairing || uploading) return;
    const result = await DocumentPicker.getDocumentAsync({ copyToCacheDirectory: true, multiple: false });
    if (result.canceled) return;
    setUploading(true); setActivity('Uploading');
    try {
      const asset = result.assets[0];
      await uploadUri(pairing, asset.uri, asset.name);
      addEvent({ kind: 'info', payload: `${asset.name} sent to VANTA.`, at: new Date().toISOString() });
    } catch (caught) {
      addEvent({ kind: 'error', payload: caught instanceof Error ? caught.message : 'Upload failed.', at: new Date().toISOString() });
    } finally { setUploading(false); setActivity('Ready'); }
  };

  const toggleRecording = async () => {
    if (!pairing) return;
    if (recorderState.isRecording) {
      await recorder.stop();
      const uri = recorder.uri;
      if (!uri) return;
      setActivity('Transcribing');
      try { await uploadUri(pairing, uri, Platform.OS === 'web' ? 'voice.webm' : 'voice.m4a', '/api/voice'); }
      catch (caught) { addEvent({ kind: 'error', payload: caught instanceof Error ? caught.message : 'Voice message failed.' }); }
      return;
    }
    const permission = await AudioModule.requestRecordingPermissionsAsync();
    if (!permission.granted) {
      addEvent({ kind: 'error', payload: 'Microphone permission is required for voice input.' });
      return;
    }
    await setAudioModeAsync({ playsInSilentMode: true, allowsRecording: true });
    await recorder.prepareToRecordAsync();
    recorder.record();
    setActivity('Listening');
  };

  const disconnect = () => { savePairing(null); setPairing(null); setFeed([]); seen.current.clear(); setSettingsOpen(false); };

  const mode = status?.mode ? status.mode.replace(/_/g, ' ') : 'assistant';
  const connectionColor = connection === 'online' ? '#35d07f' : connection === 'connecting' ? '#f0b64d' : '#657080';
  const headerStatus = useMemo(() => status?.standby ? 'Standby' : activity, [status?.standby, activity]);

  if (!pairing) return <PairScreen onPaired={(value) => { setPairing(value); setConnection('connecting'); }} />;

  return (
    <SafeAreaView style={styles.page}>
      <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined} style={styles.page}>
        <View style={styles.header}>
          <View style={styles.brandRow}>
            <Image source={require('../../assets/images/vanta-icon.png')} style={styles.logo} />
            <View>
              <Text style={styles.brand}>VANTA</Text>
              <View style={styles.statusRow}>
                <View style={[styles.dot, { backgroundColor: connectionColor }]} />
                <Text style={styles.subtle}>{connection === 'online' ? headerStatus : 'Reconnecting'}</Text>
              </View>
            </View>
          </View>
          <View style={styles.headerActions}>
            <View style={styles.modePill}><Text style={styles.modeText}>{mode}</Text></View>
            <Pressable accessibilityLabel="Settings" onPress={() => setSettingsOpen(!settingsOpen)} style={styles.iconButton}>
              <Text style={styles.iconText}>•••</Text>
            </Pressable>
          </View>
        </View>

        {settingsOpen ? (
          <View style={styles.settings}>
            <View><Text style={styles.settingsTitle}>This iPhone</Text><Text style={styles.subtle}>Paired securely · token expires automatically</Text></View>
            <Pressable onPress={() => setAutoSpeak(!autoSpeak)} style={styles.settingRow}>
              <Text style={styles.settingText}>Speak responses</Text><Text style={styles.settingValue}>{autoSpeak ? 'On' : 'Off'}</Text>
            </Pressable>
            <Pressable onPress={disconnect} style={styles.settingRow}>
              <Text style={styles.disconnectText}>Disconnect phone</Text>
            </Pressable>
          </View>
        ) : null}

        <ScrollView ref={scroll} style={styles.feed} contentContainerStyle={styles.feedContent} keyboardShouldPersistTaps="handled">
          {feed.length === 0 ? (
            <View style={styles.empty}>
              <Image source={require('../../assets/images/vanta-icon.png')} style={styles.emptyLogo} />
              <Text style={styles.emptyTitle}>Ready when you are.</Text>
              <Text style={styles.emptyCopy}>Speak, type, attach a file, approve an action, or ask VANTA to create a report.</Text>
            </View>
          ) : feed.map((item) => <FeedCard key={item.key} item={item} pairing={pairing} />)}
        </ScrollView>

        <View style={styles.composerWrap}>
          <View style={[styles.composer, recorderState.isRecording && styles.recordingComposer]}>
            <Pressable onPress={pickFile} disabled={uploading} style={styles.composeButton}>
              <Text style={styles.composeIcon}>{uploading ? '…' : '+'}</Text>
            </Pressable>
            <TextInput
              value={draft} onChangeText={setDraft} multiline maxLength={8000}
              placeholder={recorderState.isRecording ? 'Listening… tap the mic to send' : 'Message VANTA'}
              placeholderTextColor="#6d7380" style={styles.composerInput}
              onSubmitEditing={() => { if (!draft.includes('\n')) submit(); }}
            />
            <Pressable onPress={toggleRecording} style={[styles.composeButton, recorderState.isRecording && styles.recordButton]}>
              <Text style={styles.micIcon}>{recorderState.isRecording ? '■' : '●'}</Text>
            </Pressable>
            <Pressable onPress={submit} disabled={!draft.trim() || sending} style={[styles.sendButton, (!draft.trim() || sending) && styles.sendDisabled]}>
              <Text style={styles.sendIcon}>↑</Text>
            </Pressable>
          </View>
          <Text style={styles.footerText}>Private connection to your VANTA computer</Text>
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: '#07080a' },
  header: { minHeight: 72, borderBottomWidth: 1, borderBottomColor: '#191c22', paddingHorizontal: 18, paddingVertical: 12, flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  brandRow: { flexDirection: 'row', alignItems: 'center', gap: 11 }, logo: { width: 39, height: 39, borderRadius: 12 },
  brand: { color: '#fff', fontSize: 14, fontWeight: '800', letterSpacing: 1.6 }, statusRow: { flexDirection: 'row', alignItems: 'center', gap: 6, marginTop: 3 },
  dot: { width: 6, height: 6, borderRadius: 3 }, subtle: { color: '#8f97a5', fontSize: 12 }, headerActions: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  modePill: { backgroundColor: '#12151a', borderColor: '#242832', borderWidth: 1, borderRadius: 999, paddingHorizontal: 11, paddingVertical: 7 },
  modeText: { color: '#bfc6d3', fontSize: 11, textTransform: 'capitalize' }, iconButton: { width: 38, height: 38, borderRadius: 12, backgroundColor: '#12151a', alignItems: 'center', justifyContent: 'center' }, iconText: { color: '#d5dae3', fontWeight: '800' },
  settings: { position: 'absolute', top: 77, right: 14, width: 285, zIndex: 20, backgroundColor: '#111318', borderColor: '#292d36', borderWidth: 1, borderRadius: 17, padding: 15, gap: 13, shadowColor: '#000', shadowOpacity: .45, shadowRadius: 24 },
  settingsTitle: { color: '#fff', fontSize: 15, fontWeight: '700', marginBottom: 3 }, settingRow: { minHeight: 43, borderTopWidth: 1, borderTopColor: '#242832', flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingTop: 12 }, settingText: { color: '#e4e8ef', fontSize: 14 }, settingValue: { color: '#58a6ff', fontSize: 13 }, disconnectText: { color: '#ff727d', fontSize: 14 },
  feed: { flex: 1 }, feedContent: { width: '100%', maxWidth: 820, alignSelf: 'center', padding: 16, gap: 12, paddingBottom: 28 },
  empty: { minHeight: 460, alignItems: 'center', justifyContent: 'center', padding: 30 }, emptyLogo: { width: 62, height: 62, borderRadius: 20, marginBottom: 20, opacity: .9 }, emptyTitle: { color: '#fff', fontSize: 23, fontWeight: '700', marginBottom: 9 }, emptyCopy: { color: '#8e96a5', maxWidth: 340, textAlign: 'center', lineHeight: 21 },
  feedCard: { alignSelf: 'stretch', backgroundColor: '#111318', borderColor: '#20242c', borderWidth: 1, borderRadius: 17, padding: 15 }, userCard: { backgroundColor: '#10223b', borderColor: '#1f416c', marginLeft: 36 }, reportShell: { backgroundColor: '#0d0f13' },
  eyebrow: { color: '#58a6ff', fontSize: 10, fontWeight: '800', letterSpacing: 1.15, marginBottom: 8 }, errorLabel: { color: '#ff727d' }, feedText: { color: '#eef1f6', fontSize: 15, lineHeight: 22 }, timestamp: { color: '#656d7a', fontSize: 10, marginTop: 10 },
  approvalCard: { backgroundColor: '#15130e', borderColor: '#4b3d1f', borderWidth: 1, borderRadius: 17, padding: 16 }, approvalText: { color: '#f5f0e6', fontSize: 15, lineHeight: 22 }, approvalActions: { flexDirection: 'row', gap: 10, justifyContent: 'flex-end', marginTop: 15 },
  primaryButton: { backgroundColor: '#f2f5f8', borderRadius: 11, paddingHorizontal: 18, paddingVertical: 11 }, primaryButtonText: { color: '#090a0d', fontWeight: '700' }, secondaryButton: { backgroundColor: '#252219', borderRadius: 11, paddingHorizontal: 18, paddingVertical: 11 }, secondaryButtonText: { color: '#e9dfc7', fontWeight: '600' }, settled: { color: '#8bd7ae', marginTop: 13, fontWeight: '700' },
  composerWrap: { borderTopWidth: 1, borderTopColor: '#191c22', backgroundColor: '#090a0d', paddingHorizontal: 12, paddingTop: 10, paddingBottom: 8 }, composer: { width: '100%', maxWidth: 820, alignSelf: 'center', minHeight: 54, maxHeight: 138, backgroundColor: '#12151a', borderColor: '#2a2e37', borderWidth: 1, borderRadius: 19, flexDirection: 'row', alignItems: 'flex-end', padding: 6 }, recordingComposer: { borderColor: '#d95661' }, composerInput: { flex: 1, color: '#f5f7fb', minHeight: 41, maxHeight: 120, paddingHorizontal: 10, paddingVertical: 10, fontSize: 15, outlineStyle: 'none' as any },
  composeButton: { width: 40, height: 40, borderRadius: 13, alignItems: 'center', justifyContent: 'center' }, composeIcon: { color: '#b7bfcc', fontSize: 26, lineHeight: 28 }, micIcon: { color: '#aab2c0', fontSize: 13 }, recordButton: { backgroundColor: '#4a1d22' }, sendButton: { width: 40, height: 40, borderRadius: 13, backgroundColor: '#edf2f8', alignItems: 'center', justifyContent: 'center' }, sendDisabled: { backgroundColor: '#252930' }, sendIcon: { color: '#080a0d', fontSize: 22, fontWeight: '800' }, footerText: { color: '#545b67', fontSize: 10, textAlign: 'center', marginTop: 6 },
  pairPage: { flex: 1, backgroundColor: '#07080a', alignItems: 'center', justifyContent: 'center', padding: 20 }, pairCard: { width: '100%', maxWidth: 430, backgroundColor: '#101217', borderColor: '#282c35', borderWidth: 1, borderRadius: 24, padding: 25 }, logoLarge: { width: 58, height: 58, borderRadius: 18, marginBottom: 22 }, pairTitle: { color: '#fff', fontSize: 27, fontWeight: '800', marginBottom: 9 }, pairCopy: { color: '#969eac', fontSize: 14, lineHeight: 21, marginBottom: 22 }, inputLabel: { color: '#c8ced8', fontSize: 12, fontWeight: '600', marginBottom: 7 }, pairInput: { color: '#fff', backgroundColor: '#090b0f', borderColor: '#292d36', borderWidth: 1, borderRadius: 12, paddingHorizontal: 13, height: 48, marginBottom: 15, outlineStyle: 'none' as any },
  connectButton: { height: 50, borderRadius: 13, backgroundColor: '#eaf2ff', alignItems: 'center', justifyContent: 'center', marginTop: 2 }, connectText: { color: '#07111f', fontWeight: '800' }, disabled: { opacity: .45 }, errorText: { color: '#ff727d', marginBottom: 12 }, pairFootnote: { color: '#68717f', fontSize: 11, lineHeight: 16, marginTop: 15 },
});
