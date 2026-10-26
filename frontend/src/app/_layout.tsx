import { useFonts } from 'expo-font';
import { DarkTheme, DefaultTheme, Stack, ThemeProvider } from 'expo-router';
import * as SplashScreen from 'expo-splash-screen';
import { useEffect } from 'react';
import { useColorScheme } from 'react-native';

import { FontAssets, FontFamily } from '@/constants/theme';

// 글꼴을 다 불러올 때까지 스플래시 화면을 유지한다 (시스템 글꼴로 잠깐 보였다 바뀌는 것 방지)
SplashScreen.preventAutoHideAsync();

export default function RootLayout() {
  const colorScheme = useColorScheme();
  const [fontsLoaded, fontError] = useFonts(FontAssets);

  useEffect(() => {
    if (fontsLoaded || fontError) SplashScreen.hideAsync();
  }, [fontsLoaded, fontError]);

  // 글꼴을 못 불러와도(fontError) 시스템 글꼴로 앱은 연다
  if (!fontsLoaded && !fontError) return null;

  return (
    <ThemeProvider value={colorScheme === 'dark' ? DarkTheme : DefaultTheme}>
      <Stack
        screenOptions={{
          headerBackTitle: '뒤로',
          headerTitleStyle: { fontFamily: FontFamily.bold },
        }}>
        <Stack.Screen name="(tabs)" options={{ headerShown: false }} />
        <Stack.Screen name="paper/[id]" options={{ title: '논문 상세' }} />
      </Stack>
    </ThemeProvider>
  );
}
