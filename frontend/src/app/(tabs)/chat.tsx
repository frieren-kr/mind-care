import { useState } from 'react';
import { Pressable, StyleSheet, TextInput, View } from 'react-native';

import { ThemedText } from '@/components/themed-text';
import { Chip, Disclaimer, EvidenceBadge, Screen } from '@/components/ui';
import { MinTouch, Radius, Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';
import { mockChat, suggestedQuestions } from '@/mocks/user';
import type { ChatMessage } from '@/types';

export default function ChatScreen() {
  const theme = useTheme();
  const [messages, setMessages] = useState<ChatMessage[]>(mockChat);
  const [input, setInput] = useState('');

  const send = (text: string) => {
    if (!text.trim()) return;
    // 백엔드 연동 전: 고정 안내 답변
    setMessages((m) => [
      ...m,
      { id: `u${m.length}`, role: 'user', text },
      { id: `a${m.length}`, role: 'assistant', text: '(목업) 백엔드 연동 후 근거와 출처가 달린 답변이 표시됩니다.' },
    ]);
    setInput('');
  };

  return (
    <View style={[styles.container, { backgroundColor: theme.background }]}>
      <Screen>
        {messages.map((m) => (
          <View
            key={m.id}
            style={[
              styles.bubble,
              m.role === 'user'
                ? [styles.user, { backgroundColor: theme.primary }]
                : [styles.assistant, { backgroundColor: theme.backgroundElement, borderColor: theme.border }],
            ]}>
            <ThemedText style={m.role === 'user' && { color: theme.onPrimary }}>{m.text}</ThemedText>
            {m.citations && (
              <View style={styles.citations}>
                <ThemedText type="smallBold">📚 출처</ThemedText>
                {m.citations.map((c) => (
                  <View key={c.no} style={styles.citation}>
                    <ThemedText type="small">
                      [{c.no}] {c.title}
                    </ThemedText>
                    <EvidenceBadge level={c.evidence_level} />
                  </View>
                ))}
              </View>
            )}
          </View>
        ))}
        <Disclaimer text="답변은 의학적 진단이나 치료를 대신하지 않습니다." />
      </Screen>

      <View style={[styles.composer, { backgroundColor: theme.backgroundElement, borderColor: theme.border }]}>
        <View style={styles.suggestions}>
          {suggestedQuestions.map((s) => (
            <Chip key={s} label={s} selected={false} onPress={() => send(s)} />
          ))}
        </View>
        <View style={styles.inputRow}>
          <TextInput
            value={input}
            onChangeText={setInput}
            onSubmitEditing={() => send(input)}
            placeholder="질문을 입력하세요"
            placeholderTextColor={theme.textSecondary}
            accessibilityLabel="질문 입력"
            style={[styles.input, { color: theme.text, borderColor: theme.border }]}
          />
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="보내기"
            onPress={() => send(input)}
            style={[styles.send, { backgroundColor: theme.primary }]}>
            <ThemedText type="bold" style={{ color: theme.onPrimary }}>
              보내기
            </ThemedText>
          </Pressable>
        </View>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
  },
  bubble: {
    maxWidth: '88%',
    borderRadius: Radius.card,
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
  citations: {
    gap: Spacing.one,
  },
  citation: {
    gap: Spacing.half,
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
    gap: Spacing.two,
  },
  input: {
    flex: 1,
    minHeight: MinTouch,
    borderWidth: 1,
    borderRadius: Radius.card,
    paddingHorizontal: Spacing.three,
    fontSize: 18,
  },
  send: {
    minHeight: MinTouch,
    paddingHorizontal: Spacing.three,
    borderRadius: Radius.card,
    alignItems: 'center',
    justifyContent: 'center',
  },
});
