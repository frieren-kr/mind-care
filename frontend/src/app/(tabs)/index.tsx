import { router, useFocusEffect, type Href } from 'expo-router';
import { SymbolView, type SymbolViewProps } from 'expo-symbols';
import { useCallback, useRef } from 'react';
import { Pressable, StyleSheet, View } from 'react-native';

import { currentUserId, getRecommendations, getUserProfile } from '@/api';
import { PaperCard } from '@/components/paper-card';
import { SkeletonPaperCard } from '@/components/skeleton';
import { ThemedText } from '@/components/themed-text';
import { TodayCare } from '@/components/today-care';
import { DoubleRule, ErrorView, Screen, SectionLabel } from '@/components/ui';
import { MinTouch, Spacing } from '@/constants/theme';
import { useApi } from '@/hooks/use-api';
import { useTheme } from '@/hooks/use-theme';
import { todayLabel } from '@/utils/date';
import { feedCaption } from '@/utils/feed';

type Shortcut = {
  label: string;
  caption: string;
  icon: SymbolViewProps['name'];
  href?: Href;
};

// 아이콘: iOS는 SF Symbols, Android·웹은 Material Symbols 이름 (탭바와 같다)
const shortcuts: Shortcut[] = [
  {
    label: '자가점검',
    caption: '15문항 · 5분',
    icon: { ios: 'brain.head.profile', android: 'psychology', web: 'psychology' },
    href: '/check',
  },
  {
    label: '챗봇에게 묻기',
    caption: '논문·지침 근거로 답해요',
    icon: { ios: 'bubble.left.and.bubble.right', android: 'chat', web: 'chat' },
    href: '/chat',
  },
  {
    label: '돌봄 기록',
    caption: '오늘 있었던 일 남기기',
    icon: { ios: 'list.bullet.rectangle', android: 'edit_note', web: 'edit_note' },
    href: '/care',
  },
  {
    label: '내 정보',
    caption: '돌보는 분 관리',
    icon: { ios: 'person.crop.circle', android: 'person', web: 'person' },
    href: '/my',
  },
];

/** 2×2 바로가기 한 칸. 상자 대신 가는 선으로 나눈다 */
function ShortcutTile({ item, index }: { item: Shortcut; index: number }) {
  const theme = useTheme();
  const left = index % 2 === 0;
  const top = index < 2;
  const { href } = item;
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={`${item.label}, ${item.caption}`}
      accessibilityState={{ disabled: !href }}
      disabled={!href}
      onPress={() => href && router.navigate(href)}
      style={({ pressed }) => [
        styles.tile,
        left ? styles.tileLeft : styles.tileRight,
        { borderColor: theme.border },
        left && { borderRightWidth: StyleSheet.hairlineWidth },
        top && { borderBottomWidth: StyleSheet.hairlineWidth },
        pressed && { backgroundColor: theme.backgroundSelected },
        !href && { opacity: 0.55 },
      ]}>
      <SymbolView name={item.icon} tintColor={theme.text} size={26} />
      <ThemedText type="bold">{item.label}</ThemedText>
      <ThemedText type="small" themeColor="textSecondary">
        {item.caption}
      </ThemedText>
    </Pressable>
  );
}

export default function HomeScreen() {
  const theme = useTheme();
  const loadProfile = useCallback(() => getUserProfile(currentUserId()), []);
  const profile = useApi(loadProfile);
  // 피드 탭과 같은 추천. AI 결과는 서버에 저장돼 있어(기본 3일) 다시 만들지 않는다
  const loadFeed = useCallback(() => getRecommendations(currentUserId()), []);
  const feed = useApi(loadFeed);
  // 자가점검 저장·진료 기록 뒤 홈으로 돌아오면 '오늘의 돌봄'을 새로 (처음 열 때는 useApi가 이미 불러온다)
  const firstFocus = useRef(true);
  const reloadProfile = profile.reload;
  useFocusEffect(
    useCallback(() => {
      if (firstFocus.current) {
        firstFocus.current = false;
        return;
      }
      reloadProfile();
    }, [reloadProfile]),
  );

  const reloadAll = () => {
    profile.reload();
    feed.reload();
  };

  const me = profile.data;
  const top = feed.data?.items[0];
  const count = feed.data?.items.length ?? 0;

  return (
    <Screen refreshing={(profile.loading && me !== null) || (feed.loading && feed.data !== null)} onRefresh={reloadAll}>
      {/* 신문 1면 머리말: 날짜 + 이중선 */}
      <View style={styles.masthead}>
        <ThemedText type="small" themeColor="textSecondary">
          {todayLabel()}
        </ThemedText>
        <DoubleRule />
      </View>

      <View style={styles.greeting}>
        <ThemedText type="title">{me ? `안녕하세요, ${me.name}님` : '안녕하세요'}</ThemedText>
        {count > 0 ? (
          <ThemedText themeColor="textSecondary">돌보시는 분께 맞춘 새 연구 {count}편이 있어요.</ThemedText>
        ) : null}
      </View>
      {profile.error ? <ErrorView message={profile.error.message} onRetry={() => profile.reload()} /> : null}

      {/* 매일 하는 돌봄이 먼저, 그 아래 맞춤 연구 */}
      {me ? <TodayCare patients={me.patients} /> : null}

      <View style={styles.section}>
        <SectionLabel>오늘의 맞춤 연구</SectionLabel>
        {feed.data ? (
          <ThemedText type="small" themeColor="textSecondary">
            {feedCaption(feed.data)}
          </ThemedText>
        ) : null}
        {feed.loading && feed.data === null ? <SkeletonPaperCard /> : null}
        {feed.error && !profile.error ? (
          <ErrorView message={feed.error.message} onRetry={() => feed.reload()} />
        ) : null}
        {top ? <PaperCard item={top} /> : null}
        {feed.data && count === 0 ? (
          <ThemedText themeColor="textSecondary">아직 추천할 연구를 찾지 못했어요</ThemedText>
        ) : null}
        {count > 1 ? (
          <Pressable
            accessibilityRole="button"
            onPress={() => router.navigate('/feed')}
            style={({ pressed }) => [
              styles.moreRow,
              { borderColor: theme.rule },
              pressed && { backgroundColor: theme.backgroundSelected },
            ]}>
            <ThemedText type="bold">추천 연구 {count}편 모두 보기</ThemedText>
            <ThemedText type="bold" aria-hidden>
              ›
            </ThemedText>
          </Pressable>
        ) : null}
      </View>

      <View style={[styles.grid, { borderTopColor: theme.rule }]}>
        {shortcuts.map((item, index) => (
          <ShortcutTile key={item.label} item={item} index={index} />
        ))}
      </View>
    </Screen>
  );
}

const styles = StyleSheet.create({
  masthead: {
    gap: Spacing.one,
  },
  greeting: {
    gap: Spacing.one,
  },
  section: {
    gap: Spacing.two,
  },
  moreRow: {
    minHeight: MinTouch,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    borderTopWidth: 1,
    borderBottomWidth: 1,
  },
  grid: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    borderTopWidth: 1,
  },
  tile: {
    width: '50%',
    gap: Spacing.two,
    paddingVertical: Spacing.three,
  },
  tileLeft: {
    paddingRight: Spacing.three,
  },
  tileRight: {
    paddingLeft: Spacing.three,
  },
});
