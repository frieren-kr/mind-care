import { useFonts } from 'expo-font';
import { DarkTheme, DefaultTheme, Stack, ThemeProvider } from 'expo-router';
import * as SplashScreen from 'expo-splash-screen';
import { StatusBar } from 'expo-status-bar';
import { useEffect } from 'react';

import { Colors, FontAssets, FontFamily } from '@/constants/theme';
import { AuthProvider, useAuth } from '@/hooks/use-auth';
import { TextScaleProvider, useTextScale } from '@/hooks/use-text-scale';
import { ThemeModeProvider, useResolvedColorScheme, useThemeMode } from '@/hooks/use-theme-mode';

// 글꼴·글자 크기·화면 모드 설정·로그인 세션을 다 불러올 때까지 스플래시 화면을 유지한다
// (시스템 글꼴·기본 크기로 잠깐 보였다 바뀌는 것 방지)
SplashScreen.preventAutoHideAsync();

export default function RootLayout() {
  return (
    <AuthProvider>
      <ThemeModeProvider>
        <TextScaleProvider>
          <AppShell />
        </TextScaleProvider>
      </ThemeModeProvider>
    </AuthProvider>
  );
}

/** 화면 바탕·머리글·구분선을 앱 테마(신문 지면)에 맞춘 내비게이션 테마 */
function navigationTheme(scheme: 'light' | 'dark') {
  const base = scheme === 'dark' ? DarkTheme : DefaultTheme;
  const c = Colors[scheme];
  return {
    ...base,
    colors: { ...base.colors, background: c.background, card: c.background, text: c.text, border: c.border, primary: c.primary },
  };
}

function AppShell() {
  const colorScheme = useResolvedColorScheme();
  const { ready: themeModeReady } = useThemeMode();
  const [fontsLoaded, fontError] = useFonts(FontAssets);
  const { ready: textScaleReady } = useTextScale();
  const { session, ready: authReady } = useAuth();
  // 글꼴을 못 불러와도(fontError) 시스템 글꼴로 앱은 연다
  const ready = (fontsLoaded || !!fontError) && textScaleReady && themeModeReady && authReady;

  useEffect(() => {
    if (ready) SplashScreen.hideAsync();
  }, [ready]);

  if (!ready) return null;

  return (
    <ThemeProvider value={navigationTheme(colorScheme)}>
      {/* 앱 안에서 어둡게를 고르면 폰이 밝은 모드여도 상태 표시줄 글자를 밝게 */}
      <StatusBar style={colorScheme === 'dark' ? 'light' : 'dark'} />
      <Stack
        screenOptions={{
          headerBackTitle: '뒤로',
          headerTitleStyle: { fontFamily: FontFamily.bold },
          headerShadowVisible: false,
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
