import { router } from 'expo-router';

import { ThemedText } from '@/components/themed-text';
import { Button, Card, Screen } from '@/components/ui';
import { mockDashboard } from '@/mocks/user';

const checkLevelLabel = { normal: '양호', caution: '주의', visit: '전문 검사 권장' } as const;

export default function DashboardScreen() {
  const d = mockDashboard;
  return (
    <Screen>
      <ThemedText type="title">안녕하세요, {d.user_name}님</ThemedText>

      <Card>
        <ThemedText type="subtitle">📰 이번 주 새 연구 {d.new_papers_count}편</ThemedText>
        <ThemedText themeColor="textSecondary">{d.top_paper.title}</ThemedText>
        <Button label="피드 보기" onPress={() => router.navigate('/feed')} />
      </Card>

      <Card>
        <ThemedText type="subtitle">🧠 자가점검</ThemedText>
        {d.last_self_check ? (
          <ThemedText themeColor="textSecondary">
            최근 {d.last_self_check.date} · {checkLevelLabel[d.last_self_check.level]}
            {'\n'}다음 권장일 {d.next_check_date}
          </ThemedText>
        ) : (
          <ThemedText themeColor="textSecondary">아직 점검 기록이 없어요</ThemedText>
        )}
        <Button label="점검하기" variant="secondary" onPress={() => router.navigate('/check')} />
      </Card>

      <Card>
        <ThemedText type="subtitle">📝 오늘의 돌봄 기록</ThemedText>
        <ThemedText themeColor="textSecondary">
          {d.today_log_written ? '오늘 기록을 남겼어요' : '오늘 어머니의 상태를 기록해 보세요'}
        </ThemedText>
        {/* 돌봄 기록 화면은 2주차 이후 구현 */}
        <Button label="기록하기 (준비 중)" variant="secondary" disabled />
      </Card>

      <Card>
        <ThemedText type="subtitle">💬 궁금한 점을 물어보세요</ThemedText>
        <Button label="챗봇에게 묻기" variant="secondary" onPress={() => router.navigate('/chat')} />
      </Card>
    </Screen>
  );
}

