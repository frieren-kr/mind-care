import { router, type Href } from 'expo-router';
import { SymbolView, type SymbolViewProps } from 'expo-symbols';
import { useCallback } from 'react';
import { Pressable, StyleSheet, View } from 'react-native';

import { currentUserId, getRecommendations, getUserProfile } from '@/api';
import { PaperCard } from '@/components/paper-card';
import { SkeletonPaperCard } from '@/components/skeleton';
import { ThemedText } from '@/components/themed-text';
import { DoubleRule, ErrorView, Screen, SectionLabel } from '@/components/ui';
import { MinTouch, Spacing } from '@/constants/theme';
import { useApi } from '@/hooks/use-api';
import { useTheme } from '@/hooks/use-theme';
import type { UserProfileContext } from '@/types';
import { formatDate, todayLabel } from '@/utils/date';
import { feedCaption } from '@/utils/feed';

/** 관리 환자 전체에서 가장 가까운 진료 예약 (visit_date는 'YYYY-MM-DD'라 문자열 비교로 정렬된다) */
function nextVisit(profile: UserProfileContext) {
  const visits = profile.patients.flatMap(({ patient, upcoming_visits }, index) =>
    upcoming_visits.map((visit) => ({ visit, name: patient.name || `환자 ${index + 1}` })),
  );
  return visits.sort((a, b) => a.visit.visit_date.localeCompare(b.visit.visit_date))[0] ?? null;
}

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
  // 돌봄 기록 화면은 아직 없다
  { label: '돌봄 기록', caption: '준비 중', icon: { ios: 'list.bullet.rectangle', android: 'edit_note', web: 'edit_note' } },
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

  const reloadAll = () => {
    profile.reload();
    feed.reload();
  };

  const me = profile.data;
  const top = feed.data?.items[0];
  const visit = me ? nextVisit(me) : null;
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

      {me ? (
        <View style={styles.section}>
          <SectionLabel>돌보는 분</SectionLabel>
          {me.patients.length === 0 ? (
            <ThemedText themeColor="textSecondary">아직 등록된 분이 없어요.</ThemedText>
          ) : (
            me.patients.map(({ patient }, index) => (
              <View key={patient.id} style={[styles.patientRow, { borderBottomColor: theme.border }]}>
                <ThemedText type="bold">{patient.name || `환자 ${index + 1}`}</ThemedText>
                <ThemedText type="small" themeColor="textSecondary" numberOfLines={1} style={styles.patientMeta}>
                  {[patient.dementia_stage, patient.symptoms.slice(0, 2).join(', ')].filter(Boolean).join(' · ')}
                </ThemedText>
              </View>
            ))
          )}
          {visit ? (
            <ThemedText type="small" themeColor="textSecondary">
              다음 진료: {formatDate(visit.visit.visit_date)} {visit.name}
              {visit.visit.hospital_name ? ` · ${visit.visit.hospital_name}` : ''}
            </ThemedText>
          ) : null}
        </View>
      ) : null}

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
  patientRow: {
    flexDirection: 'row',
    alignItems: 'baseline',
    gap: Spacing.three,
    paddingVertical: Spacing.two,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  patientMeta: {
    flex: 1,
    textAlign: 'right',
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
