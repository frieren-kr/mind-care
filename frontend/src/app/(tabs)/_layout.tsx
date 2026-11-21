import { Tabs } from 'expo-router';
import { SymbolView, type SymbolViewProps } from 'expo-symbols';
import { StyleSheet, Text, View, type ColorValue } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { FontFamily, Radius, Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';

/**
 * 탭 아이콘. 지금 있는 탭은 먹색 알약 바탕 + 흰 아이콘으로 칠해서 다른 탭과 한눈에 구분한다
 * (색만 조금 다르면 구분이 어렵다는 의견, 11/21 주현 님)
 */
function TabIconView({ name, color, focused }: { name: SymbolViewProps['name']; color: ColorValue; focused: boolean }) {
  const theme = useTheme();
  return (
    <View style={[styles.pill, focused && { backgroundColor: theme.primary }]}>
      <SymbolView name={name} tintColor={focused ? theme.onPrimary : color} size={24} />
    </View>
  );
}

// 아이콘: iOS는 SF Symbols, Android·웹은 Material Symbols 이름
const TabIcon = (name: SymbolViewProps['name']) =>
  function Icon({ color, focused }: { color: ColorValue; focused: boolean }) {
    return <TabIconView name={name} color={color} focused={focused} />;
  };

/** 탭 이름: 지금 탭은 굵은 먹색, 나머지는 보통 굵기 회색 */
function TabLabel({ focused, color, children }: { focused: boolean; color: ColorValue; children: string }) {
  return (
    <Text style={[styles.label, { color, fontFamily: focused ? FontFamily.bold : FontFamily.regular }]}>{children}</Text>
  );
}

export default function TabLayout() {
  const theme = useTheme();
  const insets = useSafeAreaInsets();
  return (
    <Tabs
      screenOptions={{
        tabBarActiveTintColor: theme.primary,
        tabBarInactiveTintColor: theme.textSecondary,
        tabBarLabel: TabLabel,
        // 기본 높이에는 26px 아이콘 + 13px 글자가 다 들어가지 않아 글자 아래쪽이 잘린다.
        // 높이를 직접 주면 하단 시스템 영역(insets.bottom)이 빠지므로 같이 더한다
        // 신문 지면 테마: 탭바·머리글도 종이색 바탕, 탭바 위는 먹색 선
        tabBarStyle: {
          backgroundColor: theme.background,
          borderTopColor: theme.rule,
          borderTopWidth: 1,
          height: 68 + insets.bottom,
          paddingTop: Spacing.one,
          paddingBottom: Spacing.two + insets.bottom,
        },
        headerStyle: { backgroundColor: theme.background },
        headerShadowVisible: false,
        headerTintColor: theme.text,
        // 화면 이름은 '논문 상세' 같은 다른 화면 머리글과 같은 Pretendard Bold (명조는 기사 제목에만)
        headerTitleStyle: { fontSize: 20, fontFamily: FontFamily.bold },
        sceneStyle: { backgroundColor: theme.background },
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

const styles = StyleSheet.create({
  // 머티리얼 3 내비게이션 바의 '선택 표시'와 같은 크기 (아이콘 24 + 위아래 4)
  pill: {
    width: 56,
    height: 32,
    borderRadius: Radius.pill,
    alignItems: 'center',
    justifyContent: 'center',
  },
  label: {
    fontSize: 13,
    lineHeight: 18,
    marginTop: Spacing.half,
  },
});
