import { useFonts } from 'expo-font';
import { DarkTheme, DefaultTheme, Stack, ThemeProvider } from 'expo-router';
import * as SplashScreen from 'expo-splash-screen';
import { useEffect } from 'react';
import { useColorScheme } from 'react-native';

import { FontAssets, FontFamily } from '@/constants/theme';
import { AuthProvider, useAuth } from '@/hooks/use-auth';
import { TextScaleProvider, useTextScale } from '@/hooks/use-text-scale';

// 글꼴·글자 크기 설정·로그인 세션을 다 불러올 때까지 스플래시 화면을 유지한다
// (시스템 글꼴·기본 크기로 잠깐 보였다 바뀌는 것 방지)
SplashScreen.preventAutoHideAsync();

export default function RootLayout() {
  return (
    <AuthProvider>
      <TextScaleProvider>
        <AppShell />
      </TextScaleProvider>
    </AuthProvider>
  );
}

function AppShell() {
  const colorScheme = useColorScheme();
  const [fontsLoaded, fontError] = useFonts(FontAssets);
  const { ready: textScaleReady } = useTextScale();
  const { session, ready: authReady } = useAuth();
  // 글꼴을 못 불러와도(fontError) 시스템 글꼴로 앱은 연다
  const ready = (fontsLoaded || !!fontError) && textScaleReady && authReady;

  useEffect(() => {
    if (ready) SplashScreen.hideAsync();
  }, [ready]);

  if (!ready) return null;

  return (
    <ThemeProvider value={colorScheme === 'dark' ? DarkTheme : DefaultTheme}>
      <Stack
        screenOptions={{
          headerBackTitle: '뒤로',
          headerTitleStyle: { fontFamily: FontFamily.bold },
        }}>
        {/* 로그인 전에는 로그인 화면만, 로그인 후에는 나머지 화면만 열린다. 토큰이 만료되면(401) 자동으로 로그인 화면으로 */}
        <Stack.Protected guard={session !== null}>
          <Stack.Screen name="(tabs)" options={{ headerShown: false }} />
          <Stack.Screen name="paper/[id]" options={{ title: '논문 상세' }} />
          <Stack.Screen name="patient/form" options={{ title: '돌보는 분' }} />
        </Stack.Protected>
        <Stack.Protected guard={session === null}>
          <Stack.Screen name="login" options={{ headerShown: false }} />
          <Stack.Screen name="signup" options={{ title: '계정 만들기' }} />
        </Stack.Protected>
      </Stack>
    </ThemeProvider>
  );
}
