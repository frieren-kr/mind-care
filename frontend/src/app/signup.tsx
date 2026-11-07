import { router } from 'expo-router';
import { useState } from 'react';
import { KeyboardAvoidingView, Platform, StyleSheet } from 'react-native';

import { PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH, signup } from '@/api';
import { toApiError } from '@/api/client';
import { ThemedText } from '@/components/themed-text';
import { Button, Card, Chip, Disclaimer, Screen, TextField } from '@/components/ui';
import { useTheme } from '@/hooks/use-theme';

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const PHONE_PATTERN = /^01\d{8,9}$/;

/** 휴대폰 번호는 서버가 '-'·공백을 지우고 저장하지만, 화면에서도 미리 형식을 알려 준다 */
function normalizePhone(value: string): string {
  return value.replace(/[\s\-()]/g, '');
}

/** 화면에서 먼저 막을 입력 오류. 서버 검증(user_schemas.UserIn)과 같은 기준으로 맞춘다 */
function validate(form: { name: string; email: string; phone: string; password: string; confirm: string }) {
  if (!form.name.trim()) return '이름을 입력해 주세요.';
  if (!EMAIL_PATTERN.test(form.email.trim())) return '이메일 형식을 확인해 주세요.';
  if (form.phone && !PHONE_PATTERN.test(normalizePhone(form.phone))) return '휴대폰 번호를 확인해 주세요. (예: 010-1234-5678)';
  if (form.password.length < PASSWORD_MIN_LENGTH) return `비밀번호는 ${PASSWORD_MIN_LENGTH}자 이상으로 정해 주세요.`;
  if (form.password !== form.confirm) return '비밀번호가 서로 달라요.';
  return null;
}

/**
 * 계정 만들기 (POST /auth/signup, ★ 백엔드 API 준비 전). 가입하면 바로 로그인돼 탭 화면으로 넘어간다.
 * 돌보는 분 정보는 가입 뒤 내 정보 > 돌보는 분 추가에서 입력한다.
 */
export default function SignupScreen() {
  const theme = useTheme();
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [phone, setPhone] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [agreed, setAgreed] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    if (submitting) return;
    const invalid = validate({ name, email, phone, password, confirm });
    if (invalid) {
      setError(invalid);
      return;
    }
    if (!agreed) {
      setError('개인정보 수집·이용에 동의해 주세요.');
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await signup({
        name: name.trim(),
        email: email.trim(),
        password,
        phone_number: phone ? normalizePhone(phone) : null,
      });
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
        <ThemedText type="title">계정 만들기</ThemedText>
        <ThemedText themeColor="textSecondary">
          가입한 뒤 돌보는 분의 정보를 넣으면 그분께 맞는 연구를 골라 드려요.
        </ThemedText>

        <Card>
          <ThemedText type="smallBold">이름</ThemedText>
          <TextField value={name} onChangeText={setName} placeholder="홍길동" accessibilityLabel="이름"
            autoComplete="name" textContentType="name" maxLength={50} editable={!submitting} />

          <ThemedText type="smallBold">이메일</ThemedText>
          <TextField value={email} onChangeText={setEmail} placeholder="example@email.com" accessibilityLabel="이메일"
            autoCapitalize="none" autoCorrect={false} autoComplete="email" keyboardType="email-address"
            textContentType="emailAddress" maxLength={320} editable={!submitting} />

          <ThemedText type="smallBold">휴대폰 번호 (선택)</ThemedText>
          <TextField value={phone} onChangeText={setPhone} placeholder="010-1234-5678" accessibilityLabel="휴대폰 번호"
            keyboardType="phone-pad" autoComplete="tel" textContentType="telephoneNumber" maxLength={20}
            editable={!submitting} />

          <ThemedText type="smallBold">비밀번호</ThemedText>
          <TextField value={password} onChangeText={setPassword} placeholder={`${PASSWORD_MIN_LENGTH}자 이상`}
            accessibilityLabel="비밀번호" secureTextEntry autoCapitalize="none" autoCorrect={false}
            autoComplete="new-password" textContentType="newPassword" maxLength={PASSWORD_MAX_LENGTH}
            editable={!submitting} />

          <ThemedText type="smallBold">비밀번호 확인</ThemedText>
          <TextField value={confirm} onChangeText={setConfirm} placeholder="한 번 더 입력" accessibilityLabel="비밀번호 확인"
            secureTextEntry autoCapitalize="none" autoCorrect={false} autoComplete="new-password"
            textContentType="newPassword" maxLength={PASSWORD_MAX_LENGTH} returnKeyType="go"
            onSubmitEditing={submit} editable={!submitting} />
        </Card>

        {/* ★ 동의 문구는 초안이다. 보관 기간·탈퇴 시 삭제 등은 백엔드 정책이 정해지면 채운다 */}
        <Card>
          <ThemedText type="smallBold">개인정보 수집·이용 동의 (필수)</ThemedText>
          <ThemedText type="small" themeColor="textSecondary">
            이름·이메일·휴대폰 번호와, 가입 뒤 입력하는 돌보는 분의 건강 정보(진단·증상·복약 등)를 맞춤 연구 추천과
            챗봇 답변에만 사용해요.
          </ThemedText>
          <Chip label={agreed ? '동의했어요' : '동의합니다'} selected={agreed} onPress={() => setAgreed(!agreed)} />
        </Card>

        {error ? (
          <ThemedText accessibilityRole="alert" style={{ color: theme.accent }}>
            {error}
          </ThemedText>
        ) : null}
        <Button label={submitting ? '만드는 중…' : '가입하고 시작하기'} onPress={submit} disabled={submitting} />
        <Button label="이미 계정이 있어요" variant="secondary" onPress={() => (router.canGoBack() ? router.back() : router.replace('/login'))} disabled={submitting} />
        <Disclaimer text="이 앱은 의료 진단을 대신하지 않아요." />
      </Screen>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  flex: {
    flex: 1,
  },
});
