import AsyncStorage from '@react-native-async-storage/async-storage';
import { router } from 'expo-router';
import { useEffect, useState } from 'react';
import { KeyboardAvoidingView, Platform, StyleSheet, View } from 'react-native';

import { login } from '@/api';
import { toApiError } from '@/api/client';
import { ThemedText } from '@/components/themed-text';
import { Button, Card, Checkbox, Disclaimer, Screen, SectionLabel, TextField } from '@/components/ui';
import { Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';

/** '아이디 저장'을 켜면 이메일만 이 기기에 저장한다. 비밀번호는 저장하지 않는다 */
const SAVED_EMAIL_KEY = 'mindcare.savedEmail';

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
  const [rememberEmail, setRememberEmail] = useState(false);

  // 저장해 둔 이메일이 있으면 채우고 '아이디 저장'을 켜 둔다
  useEffect(() => {
    let active = true;
    AsyncStorage.getItem(SAVED_EMAIL_KEY)
      .then((saved) => {
        if (!active || !saved) return;
        setEmail((current) => current || saved);
        setRememberEmail(true);
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, []);

  const canSubmit = email.trim().length > 0 && password.length > 0 && !submitting;

  const submit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      await login(email.trim(), password);
      // 로그인에 성공했을 때만 저장·삭제한다 (틀린 이메일을 저장하지 않게)
      if (rememberEmail) AsyncStorage.setItem(SAVED_EMAIL_KEY, email.trim()).catch(() => {});
      else AsyncStorage.removeItem(SAVED_EMAIL_KEY).catch(() => {});
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
          <Checkbox label="아이디 저장" checked={rememberEmail} onChange={setRememberEmail} disabled={submitting} />
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

        <Disclaimer text="보안을 위해 로그인은 일정 시간이 지나면 풀려요. 아이디 저장을 켜 두면 비밀번호만 다시 입력하면 돼요." />
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
