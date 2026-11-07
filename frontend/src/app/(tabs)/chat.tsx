import { useLocalSearchParams } from 'expo-router';
import * as WebBrowser from 'expo-web-browser';
import { useCallback, useEffect, useRef, useState } from 'react';
import { ActivityIndicator, Pressable, ScrollView, StyleSheet, View } from 'react-native';

import { currentUserId, getChatHistory, getUserProfile, sendChatMessage } from '@/api';
import { toApiError } from '@/api/client';
import { ThemedText } from '@/components/themed-text';
import { Button, Chip, Disclaimer, EvidenceBadge, SectionLabel, TextField } from '@/components/ui';
import { studyTypeLabel } from '@/constants/labels';
import { MaxContentWidth, Radius, Spacing } from '@/constants/theme';
import { useApi } from '@/hooks/use-api';
import { useTheme } from '@/hooks/use-theme';
import type { ChatHistoryTurn, ChatMessage, ChatSource } from '@/types';

/** 지난 대화를 한 번에 불러오는 턴 수 (서버 최대 50) */
const HISTORY_PAGE = 20;

/** 대화별 지난 대화 불러오기 상태 */
type HistoryState = {
  status: 'loading' | 'done' | 'error';
  hasMore: boolean;
  /** 가장 오래된 턴의 답변 시각 = 더 불러오기의 before */
  oldest?: string;
  loadingMore?: boolean;
};

/** 저장된 질문·답변 쌍 → 말풍선 두 개 */
function turnsToMessages(turns: ChatHistoryTurn[]): ChatMessage[] {
  return turns.flatMap((t) => [
    { id: `h-${t.turn_id}-q`, role: 'user' as const, text: t.question, at: t.asked_at },
    { id: `h-${t.turn_id}-a`, role: 'assistant' as const, text: t.answer, sources: t.sources, at: t.answered_at },
  ]);
}

/** 날짜 구분선 글자: '11월 7일 (토)'. 시각이 없거나 잘못되면 null */
function dayLabel(iso: string | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return `${d.getMonth() + 1}월 ${d.getDate()}일 (${'일월화수목금토'[d.getDay()]})`;
}

const suggestedQuestions = ['밤에 잠을 안 주무세요', '식사를 거부하세요', '간병이 너무 지쳐요'];

/** ChatWorkflow 입력 상한 (prepare_chat_input: 공백이 아닌 4,000자 이하) */
const MAX_QUESTION_LENGTH = 4000;

// 면책 문구는 UI 고정 책임이다 (agent README)
const CHAT_DISCLAIMER = '답변은 수집된 논문과 공식 지침을 바탕으로 한 정보이며, 의학적 진단이나 치료를 대신하지 않습니다.';

/** 환자를 고르지 않은 대화의 키. 서버도 patient_id 없음끼리 대화를 이어 간다 (chat_history.py) */
const NO_PATIENT = 'none';

let messageSeq = 0;
const nextId = (prefix: string) => `${prefix}-${Date.now()}-${messageSeq++}`;

/** 근거 자료 한 건. 번호는 화면이 붙인다 (답변 본문 속 [1] 같은 표기와는 연결하지 않는다) */
function SourceItem({ source, no }: { source: ChatSource; no: number }) {
  const theme = useTheme();
  const guideline = source.evidence_type === 'guideline';
  const meta = guideline
    ? [source.organization, source.publication_year].filter(Boolean)
    : [source.journal, source.publication_year, source.study_type ? studyTypeLabel(source.study_type) : null].filter(
        Boolean,
      );
  const url = source.source_url;

  return (
    <View style={[styles.source, { borderTopColor: theme.border }]}>
      <View style={styles.sourceHead}>
        <ThemedText type="smallBold" themeColor="textSecondary">
          [{no}]
        </ThemedText>
        {guideline ? (
          <EvidenceBadge level="guideline" />
        ) : (
          <ThemedText type="smallBold" themeColor="primary">
            최신 연구
          </ThemedText>
        )}
      </View>
      <ThemedText type="small" numberOfLines={3}>
        {source.title}
      </ThemedText>
      {meta.length > 0 ? (
        <ThemedText type="small" themeColor="textSecondary">
          {meta.join(' · ')}
        </ThemedText>
      ) : null}
      {/* 원문 링크가 없으면 버튼을 숨긴다 (가짜 URL을 만들지 않는다) */}
      {url ? (
        <Pressable accessibilityRole="link" onPress={() => WebBrowser.openBrowserAsync(url)}>
          <ThemedText type="smallBold" themeColor="primary">
            원문 보기
          </ThemedText>
        </Pressable>
      ) : null}
    </View>
  );
}

function Bubble({ message, onRetry }: { message: ChatMessage; onRetry?: () => void }) {
  const theme = useTheme();
  const user = message.role === 'user';
  const sources = message.sources ?? [];

  return (
    <View
      style={[
        styles.bubble,
        user
          ? [styles.user, { backgroundColor: theme.primary }]
          : [styles.assistant, { backgroundColor: theme.backgroundElement, borderColor: theme.border }],
      ]}>
      <ThemedText style={user ? { color: theme.onPrimary } : undefined}>{message.text}</ThemedText>
      {!user && !message.failed && sources.length > 0 ? (
        <View style={styles.sources}>
          <SectionLabel>근거 자료</SectionLabel>
          {sources.map((s, i) => (
            <SourceItem key={`${s.pmid ?? s.title}-${i}`} source={s} no={i + 1} />
          ))}
        </View>
      ) : null}
      {/* 검색 도구 없이 답한 경우는 정상이지만, 근거가 없다는 걸 알려 준다 */}
      {!user && !message.failed && sources.length === 0 ? (
        <ThemedText type="small" themeColor="textSecondary">
          이 답변에는 연결된 근거 자료가 없어요.
        </ThemedText>
      ) : null}
      {message.failed && onRetry ? <Button label="다시 보내기" variant="secondary" onPress={onRetry} /> : null}
    </View>
  );
}

export default function ChatScreen() {
  const theme = useTheme();
  const scrollRef = useRef<ScrollView>(null);
  // 어느 분에 대한 질문인지. 고르면 서버가 그분의 기록(프로필·복약·진료·검사·안전사고·돌봄 기록) Tool을 붙인다.
  // undefined = 아직 안 고름 → 돌보는 분이 1명이면 그분, 아니면 '기본'(특정 환자 없이)
  const loadProfile = useCallback(() => getUserProfile(currentUserId()), []);
  const { data: profile, error: profileError } = useApi(loadProfile);
  // 돌보는 분 목록을 받아야 기본 대상(1명이면 그분)이 정해진다. 그 전에 지난 대화를 부르면 엉뚱한 대화를 한 번 더 부른다
  const profileSettled = profile !== null || profileError !== null;
  const patients = profile?.patients ?? [];
  const [chosenPatient, setChosenPatient] = useState<string | null | undefined>(undefined);
  const patientId = chosenPatient !== undefined ? chosenPatient : patients.length === 1 ? patients[0].patient.id : null;

  // 서버가 대화를 환자별로 따로 저장·기억하므로 화면도 환자별로 따로 보여 준다
  const [threads, setThreads] = useState<Record<string, ChatMessage[]>>({});
  const threadKey = patientId ?? NO_PATIENT;
  const messages = threads[threadKey] ?? [];
  const setMessages = (update: (m: ChatMessage[]) => ChatMessage[], key = threadKey) =>
    setThreads((t) => ({ ...t, [key]: update(t[key] ?? []) }));
  const [input, setInput] = useState('');
  const [pending, setPending] = useState(false);

  // 지난 대화 (GET /chat/{user_id}/history). 대화(환자)마다 처음 열 때 한 번 불러와 앞에 붙인다
  const [history, setHistory] = useState<Record<string, HistoryState>>({});
  const requested = useRef(new Set<string>());
  // 이전 대화를 위에 붙일 때는 맨 아래로 스크롤하지 않는다
  const keepScroll = useRef(false);

  const loadHistory = useCallback(async (key: string, pid: string | null, before?: string) => {
    setHistory((h) => ({
      ...h,
      [key]: before ? { ...h[key], loadingMore: true } : { status: 'loading', hasMore: false },
    }));
    try {
      const res = await getChatHistory(currentUserId(), pid, { limit: HISTORY_PAGE, before });
      const older = turnsToMessages(res.turns);
      const ids = new Set(older.map((m) => m.id));
      if (before) keepScroll.current = true;
      setThreads((t) => ({ ...t, [key]: [...older, ...(t[key] ?? []).filter((m) => !ids.has(m.id))] }));
      setHistory((h) => ({
        ...h,
        [key]: { status: 'done', hasMore: res.has_more, oldest: res.turns[0]?.answered_at ?? h[key]?.oldest },
      }));
    } catch {
      // 지난 대화를 못 불러와도 새 질문은 할 수 있다. 더 불러오기 실패면 원래 상태로 둔다
      setHistory((h) => ({
        ...h,
        [key]: before ? { ...h[key], loadingMore: false } : { status: 'error', hasMore: false },
      }));
    }
  }, []);

  useEffect(() => {
    if (!profileSettled || requested.current.has(threadKey)) return;
    requested.current.add(threadKey);
    loadHistory(threadKey, patientId);
  }, [profileSettled, threadKey, patientId, loadHistory]);

  const retryHistory = () => loadHistory(threadKey, patientId);
  const loadOlder = () => {
    const h = history[threadKey];
    if (h?.oldest && !h.loadingMore) loadHistory(threadKey, patientId, h.oldest);
  };
  const historyState = history[threadKey];

  // 논문 상세의 '이 연구에 대해 질문하기'로 들어오면 질문을 미리 채운다 (보내기는 사용자가 누른다)
  const { paperTitle } = useLocalSearchParams<{ paperTitle?: string }>();
  const [prefilledFor, setPrefilledFor] = useState<string | undefined>(undefined);
  if (paperTitle && paperTitle !== prefilledFor) {
    setPrefilledFor(paperTitle);
    setInput(`「${paperTitle}」 연구가 우리 상황에 어떤 의미가 있는지 알려 주세요.`);
  }

  const send = async (raw: string) => {
    const text = raw.trim();
    if (!text || pending) return;
    // 답을 기다리는 동안 대상은 못 바꾸지만, 혹시 바뀌어도 보낸 쪽 대화에 답을 넣는다
    const key = threadKey;
    setInput('');
    setPending(true);
    setMessages((m) => [...m.filter((x) => !x.failed), { id: nextId('u'), role: 'user', text, at: new Date().toISOString() }], key);
    try {
      const res = await sendChatMessage(currentUserId(), text, patientId);
      setMessages((m) => [...m, { id: nextId('a'), role: 'assistant', text: res.answer, sources: res.sources, at: new Date().toISOString() }], key);
    } catch (e) {
      setMessages(
        (m) => [
          ...m,
          { id: nextId('a'), role: 'assistant', text: `답변을 받지 못했어요. ${toApiError(e).message}`, failed: true },
        ],
        key,
      );
    } finally {
      setPending(false);
    }
  };

  // 실패한 답변 바로 앞의 내 질문을 다시 보낸다
  const retry = () => {
    const lastUser = [...messages].reverse().find((m) => m.role === 'user');
    if (!lastUser) return;
    setMessages((m) => m.filter((x) => x.id !== lastUser.id));
    send(lastUser.text);
  };

  // 지난 대화를 불러오는 중에는 첫 안내를 띄우지 않는다 (불러온 뒤 대화가 있으면 사라져서 깜빡인다)
  const empty = messages.length === 0 && historyState?.status !== 'loading';

  return (
    <View style={[styles.container, { backgroundColor: theme.background }]}>
      <ScrollView
        ref={scrollRef}
        contentContainerStyle={styles.list}
        onContentSizeChange={() => {
          if (keepScroll.current) {
            keepScroll.current = false;
            return;
          }
          scrollRef.current?.scrollToEnd({ animated: true });
        }}>
        {/* 지난 대화: 불러오는 중 / 실패 / 더 불러오기 / 보관 기간 안내 */}
        {historyState?.status === 'loading' ? (
          <View style={styles.historyRow}>
            <ActivityIndicator color={theme.primary} />
            <ThemedText type="small" themeColor="textSecondary">
              지난 대화를 불러오고 있어요
            </ThemedText>
          </View>
        ) : null}
        {historyState?.status === 'error' ? (
          <View style={styles.historyRow}>
            <ThemedText type="small" themeColor="textSecondary" style={styles.flex}>
              지난 대화를 불러오지 못했어요.
            </ThemedText>
            <Button label="다시 시도" variant="secondary" onPress={retryHistory} />
          </View>
        ) : null}
        {historyState?.status === 'done' && historyState.hasMore ? (
          <Button
            label={historyState.loadingMore ? '불러오는 중…' : '이전 대화 더 보기'}
            variant="secondary"
            disabled={historyState.loadingMore}
            onPress={loadOlder}
          />
        ) : null}
        {historyState?.status === 'done' && !historyState.hasMore && messages.some((m) => m.id.startsWith('h-')) ? (
          <ThemedText type="small" themeColor="textSecondary" style={styles.center}>
            대화는 최근 7일 동안 보관돼요
          </ThemedText>
        ) : null}

        {empty ? (
          <View style={styles.intro}>
            <SectionLabel>마인드 케어에게 묻기</SectionLabel>
            <ThemedText type="subtitle">돌봄 중 궁금한 점을 물어보세요</ThemedText>
            <ThemedText themeColor="textSecondary">
              수집된 논문과 공식 지침을 근거로 답하고, 답변 아래에 근거 자료를 함께 보여 드려요.
            </ThemedText>
            {patients.length > 0 ? (
              <ThemedText themeColor="textSecondary">
                아래에서 돌보는 분을 고르면 그분의 증상·복약·진료 기록을 참고해서 답해요.
              </ThemedText>
            ) : null}
          </View>
        ) : null}

        {messages.map((m, index) => {
          // 날짜가 바뀌는 곳마다 구분선
          const day = dayLabel(m.at);
          const prevDay = index > 0 ? dayLabel(messages[index - 1].at) : null;
          return (
            <View key={m.id} style={styles.messageBlock}>
              {day && day !== prevDay ? (
                <View style={styles.dayRow}>
                  <View style={[styles.dayLine, { backgroundColor: theme.border }]} />
                  <ThemedText type="small" themeColor="textSecondary">
                    {day}
                  </ThemedText>
                  <View style={[styles.dayLine, { backgroundColor: theme.border }]} />
                </View>
              ) : null}
              <Bubble message={m} onRetry={m.failed ? retry : undefined} />
            </View>
          );
        })}

        {pending ? (
          <View style={[styles.bubble, styles.assistant, { backgroundColor: theme.backgroundElement, borderColor: theme.border }]}>
            <View style={styles.pendingRow}>
              <ActivityIndicator color={theme.primary} />
              <ThemedText themeColor="textSecondary">근거를 찾아 답변을 준비하고 있어요</ThemedText>
            </View>
            <ThemedText type="small" themeColor="textSecondary">
              논문과 지침을 찾아보느라 30초쯤 걸릴 수 있어요.
            </ThemedText>
          </View>
        ) : null}

        <Disclaimer text={CHAT_DISCLAIMER} />
      </ScrollView>

      <View style={[styles.composer, { backgroundColor: theme.backgroundElement, borderColor: theme.border }]}>
        {patients.length > 0 ? (
          <View style={styles.target}>
            <ThemedText type="smallBold" themeColor="textSecondary">
              누구에 대해 묻나요?
            </ThemedText>
            <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.targetChips}>
              {patients.map(({ patient }, index) => (
                <Chip
                  key={patient.id}
                  label={patient.name || `환자 ${index + 1}`}
                  selected={patientId === patient.id}
                  onPress={() => !pending && setChosenPatient(patient.id)}
                />
              ))}
              <Chip label="기본" selected={patientId === null} onPress={() => !pending && setChosenPatient(null)} />
            </ScrollView>
          </View>
        ) : null}
        {empty ? (
          <View style={styles.suggestions}>
            {suggestedQuestions.map((s) => (
              <Chip key={s} label={s} selected={false} onPress={() => send(s)} />
            ))}
          </View>
        ) : null}
        <View style={styles.inputRow}>
          <TextField
            style={styles.input}
            value={input}
            onChangeText={setInput}
            placeholder="질문을 입력하세요"
            multiline
            maxLength={MAX_QUESTION_LENGTH}
            editable={!pending}
          />
          <View>
            <Button label="보내기" onPress={() => send(input)} disabled={pending || !input.trim()} />
          </View>
        </View>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
  },
  list: {
    padding: Spacing.three,
    paddingBottom: Spacing.four,
    gap: Spacing.three,
    width: '100%',
    maxWidth: MaxContentWidth,
    alignSelf: 'center',
  },
  historyRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.two,
  },
  flex: {
    flex: 1,
  },
  center: {
    textAlign: 'center',
  },
  messageBlock: {
    gap: Spacing.three,
  },
  dayRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.two,
  },
  dayLine: {
    flex: 1,
    height: StyleSheet.hairlineWidth,
  },
  intro: {
    gap: Spacing.two,
    paddingVertical: Spacing.two,
  },
  bubble: {
    maxWidth: '88%',
    // 챗봇 말풍선은 둥글게 (나머지 화면은 각지게)
    borderRadius: Radius.bubble,
    padding: Spacing.three,
    gap: Spacing.two,
  },
  user: {
    alignSelf: 'flex-end',
  },
  assistant: {
    alignSelf: 'flex-start',
    borderWidth: StyleSheet.hairlineWidth,
  },
  sources: {
    gap: Spacing.two,
    marginTop: Spacing.one,
  },
  source: {
    borderTopWidth: StyleSheet.hairlineWidth,
    paddingTop: Spacing.two,
    gap: Spacing.one,
  },
  sourceHead: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.two,
  },
  pendingRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.two,
  },
  composer: {
    borderTopWidth: StyleSheet.hairlineWidth,
    padding: Spacing.two,
    gap: Spacing.two,
  },
  target: {
    gap: Spacing.one,
  },
  targetChips: {
    gap: Spacing.two,
  },
  suggestions: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: Spacing.two,
  },
  inputRow: {
    flexDirection: 'row',
    alignItems: 'flex-end',
    gap: Spacing.two,
  },
  input: {
    flex: 1,
    maxHeight: 140,
    paddingVertical: Spacing.two,
  },
});
