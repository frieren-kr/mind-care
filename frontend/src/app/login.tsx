import { router } from 'expo-router';
import { useState } from 'react';
import { KeyboardAvoidingView, Platform, StyleSheet, View } from 'react-native';

import { login } from '@/api';
import { toApiError } from '@/api/client';
import { ThemedText } from '@/components/themed-text';
import { Button, Card, Disclaimer, Screen, SectionLabel, TextField } from '@/components/ui';
import { Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';

/**
 * 로그인 화면 (POST /auth/login). 회원가입·비밀번호 찾기는 백엔드 범위 밖이라 없다 (backend/AUTH_CHECK.md).
 * 로그인하면 세션이 저장되고 _layout의 Stack.Protected가 탭 화면으로 보낸다.
 */
export default function LoginScreen() {
  const theme = useTheme();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = email.trim().length > 0 && password.length > 0 && !submitting;

  const submit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      await login(email.trim(), password);
      // 성공하면 이 화면은 바로 닫히므로 상태를 되돌리지 않는다
    } catch (e) {
      setError(toApiError(e).message);
      setSubmitting(false);
    }
  };

  return (
    <KeyboardAvoidingView
      style={[styles.flex, { backgroundColor: theme.background }]}
      behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
      <Screen>
        <View style={styles.header}>
          <SectionLabel>마인드 케어</SectionLabel>
          <ThemedText type="title">로그인</ThemedText>
          <ThemedText themeColor="textSecondary">돌보는 분에게 맞춘 최신 치매 연구를 전해 드려요.</ThemedText>
        </View>

        <Card>
          <ThemedText type="smallBold">이메일</ThemedText>
          <TextField
            value={email}
            onChangeText={setEmail}
            placeholder="example@email.com"
            accessibilityLabel="이메일"
            autoCapitalize="none"
            autoCorrect={false}
            autoComplete="email"
            keyboardType="email-address"
            textContentType="username"
            maxLength={320}
            returnKeyType="next"
            editable={!submitting}
          />
          <ThemedText type="smallBold">비밀번호</ThemedText>
          <TextField
            value={password}
            onChangeText={setPassword}
            placeholder="비밀번호"
            accessibilityLabel="비밀번호"
            secureTextEntry
            autoCapitalize="none"
            autoCorrect={false}
            autoComplete="current-password"
            textContentType="password"
            maxLength={128}
            returnKeyType="go"
            onSubmitEditing={submit}
            editable={!submitting}
          />
          {error ? (
            <ThemedText accessibilityRole="alert" style={{ color: theme.accent }}>
              {error}
            </ThemedText>
          ) : null}
          <Button label={submitting ? '로그인하는 중…' : '로그인'} onPress={submit} disabled={!canSubmit} />
        </Card>

        <View style={styles.signup}>
          <ThemedText themeColor="textSecondary">처음 오셨나요?</ThemedText>
          <Button label="계정 만들기" variant="secondary" onPress={() => router.push('/signup')} disabled={submitting} />
        </View>

        <Disclaimer text="로그인은 15분 동안 유지되고, 지나면 다시 로그인해 주세요." />
      </Screen>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  flex: {
    flex: 1,
  },
  header: {
    gap: Spacing.two,
    paddingTop: Spacing.five,
  },
  signup: {
    gap: Spacing.two,
  },
});
