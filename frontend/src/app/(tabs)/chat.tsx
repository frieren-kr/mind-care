import { useLocalSearchParams } from 'expo-router';
import * as WebBrowser from 'expo-web-browser';
import { useRef, useState } from 'react';
import { ActivityIndicator, Pressable, ScrollView, StyleSheet, View } from 'react-native';

import { currentUserId, sendChatMessage } from '@/api';
import { toApiError } from '@/api/client';
import { ThemedText } from '@/components/themed-text';
import { Button, Chip, Disclaimer, EvidenceBadge, SectionLabel, TextField } from '@/components/ui';
import { studyTypeLabel } from '@/constants/labels';
import { MaxContentWidth, Radius, Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';
import type { ChatMessage, ChatSource } from '@/types';

const suggestedQuestions = ['밤에 잠을 안 주무세요', '식사를 거부하세요', '간병이 너무 지쳐요'];

/** ChatWorkflow 입력 상한 (prepare_chat_input: 공백이 아닌 4,000자 이하) */
const MAX_QUESTION_LENGTH = 4000;

// 면책 문구는 UI 고정 책임이다 (agent README)
const CHAT_DISCLAIMER = '답변은 수집된 논문과 공식 지침을 바탕으로 한 정보이며, 의학적 진단이나 치료를 대신하지 않습니다.';

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
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [pending, setPending] = useState(false);

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
    setInput('');
    setPending(true);
    setMessages((m) => [...m.filter((x) => !x.failed), { id: nextId('u'), role: 'user', text }]);
    try {
      const res = await sendChatMessage(currentUserId(), text);
      setMessages((m) => [...m, { id: nextId('a'), role: 'assistant', text: res.answer, sources: res.sources }]);
    } catch (e) {
      setMessages((m) => [
        ...m,
        { id: nextId('a'), role: 'assistant', text: `답변을 받지 못했어요. ${toApiError(e).message}`, failed: true },
      ]);
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

  const empty = messages.length === 0;

  return (
    <View style={[styles.container, { backgroundColor: theme.background }]}>
      <ScrollView
        ref={scrollRef}
        contentContainerStyle={styles.list}
        onContentSizeChange={() => scrollRef.current?.scrollToEnd({ animated: true })}>
        {empty ? (
          <View style={styles.intro}>
            <SectionLabel>마인드 케어에게 묻기</SectionLabel>
            <ThemedText type="subtitle">돌봄 중 궁금한 점을 물어보세요</ThemedText>
            <ThemedText themeColor="textSecondary">
              수집된 논문과 공식 지침을 근거로 답하고, 답변 아래에 근거 자료를 함께 보여 드려요.
            </ThemedText>
          </View>
        ) : null}

        {messages.map((m) => (
          <Bubble key={m.id} message={m} onRetry={m.failed ? retry : undefined} />
        ))}

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
