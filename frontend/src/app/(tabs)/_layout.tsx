import { Tabs } from 'expo-router';
import { SymbolView, type SymbolViewProps } from 'expo-symbols';
import type { ColorValue } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { FontFamily, Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';

// 아이콘: iOS는 SF Symbols, Android·웹은 Material Symbols 이름
const TabIcon = (name: SymbolViewProps['name']) =>
  function Icon({ color }: { color: ColorValue }) {
    return <SymbolView name={name} tintColor={color} size={26} />;
  };

export default function TabLayout() {
  const theme = useTheme();
  const insets = useSafeAreaInsets();
  return (
    <Tabs
      screenOptions={{
        tabBarActiveTintColor: theme.primary,
        tabBarInactiveTintColor: theme.textSecondary,
        tabBarLabelStyle: { fontSize: 13, lineHeight: 18, fontFamily: FontFamily.bold },
        // 기본 높이에는 26px 아이콘 + 13px 글자가 다 들어가지 않아 글자 아래쪽이 잘린다.
        // 높이를 직접 주면 하단 시스템 영역(insets.bottom)이 빠지므로 같이 더한다
        tabBarStyle: {
          backgroundColor: theme.backgroundElement,
          height: 64 + insets.bottom,
          paddingTop: Spacing.one,
          paddingBottom: Spacing.two + insets.bottom,
        },
        headerStyle: { backgroundColor: theme.backgroundElement },
        headerTintColor: theme.text,
        headerTitleStyle: { fontSize: 20, fontFamily: FontFamily.bold },
      }}>
      <Tabs.Screen
        name="index"
        options={{
          title: '홈',
          headerTitle: '마인드 케어',
          tabBarIcon: TabIcon({ ios: 'house.fill', android: 'home', web: 'home' }),
        }}
      />
      <Tabs.Screen
        name="feed"
        options={{
          title: '피드',
          headerTitle: '맞춤 연구',
          tabBarIcon: TabIcon({ ios: 'newspaper.fill', android: 'newspaper', web: 'newspaper' }),
        }}
      />
      <Tabs.Screen
        name="check"
        options={{
          title: '자가점검',
          tabBarIcon: TabIcon({ ios: 'brain.head.profile', android: 'psychology', web: 'psychology' }),
        }}
      />
      <Tabs.Screen
        name="chat"
        options={{
          title: '챗봇',
          headerTitle: '마인드 케어에게 묻기',
          tabBarIcon: TabIcon({
            ios: 'bubble.left.and.bubble.right.fill',
            android: 'chat',
            web: 'chat',
          }),
        }}
      />
      <Tabs.Screen
        name="my"
        options={{
          title: '내 정보',
          tabBarIcon: TabIcon({ ios: 'person.crop.circle', android: 'person', web: 'person' }),
        }}
      />
    </Tabs>
  );
}
