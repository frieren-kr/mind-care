import { Modal, Pressable, StyleSheet, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { ThemedText } from '@/components/themed-text';
import { MinTouch, Radius, Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';

/** id가 null이면 '기본'(특정 분 없이) */
export type PickerOption = { id: string | null; label: string; caption: string };

/**
 * 화면 머리글 오른쪽의 '시어머니 ▾' 버튼. 지금 누구에 대해 묻는지 항상 보이고, 누르면 선택 창을 연다.
 * (아래에 고정돼 답변을 가리던 칩 줄 대신, 11/23)
 */
export function PatientPickerButton({ label, onPress, disabled }: { label: string; onPress: () => void; disabled?: boolean }) {
  const theme = useTheme();
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={`질문 대상: ${label}. 바꾸려면 누르세요`}
      accessibilityState={{ disabled }}
      disabled={disabled}
      onPress={onPress}
      hitSlop={8}
      style={({ pressed }) => [
        styles.button,
        pressed && { backgroundColor: theme.backgroundSelected },
        disabled && { opacity: 0.5 },
      ]}>
      <ThemedText type="smallBold" numberOfLines={1} style={styles.buttonLabel}>
        {label}
      </ThemedText>
      <ThemedText type="smallBold" aria-hidden>
        ▾
      </ThemedText>
    </Pressable>
  );
}

/** 아래에서 올라오는 선택 창. 바깥을 누르거나 뒤로 가기로 닫힌다 */
export function PatientPickerSheet({
  visible,
  options,
  selected,
  onSelect,
  onClose,
}: {
  visible: boolean;
  options: PickerOption[];
  selected: string | null;
  onSelect: (id: string | null) => void;
  onClose: () => void;
}) {
  const theme = useTheme();
  const insets = useSafeAreaInsets();
  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={onClose} statusBarTranslucent>
      <View style={styles.backdropWrap}>
        <Pressable accessibilityLabel="닫기" style={[StyleSheet.absoluteFill, styles.backdrop]} onPress={onClose} />
        <View
          accessibilityViewIsModal
          style={[
            styles.sheet,
            { backgroundColor: theme.background, borderTopColor: theme.rule, paddingBottom: Spacing.three + insets.bottom },
          ]}>
          <ThemedText type="subtitle">누구에 대해 묻나요?</ThemedText>
          <ThemedText type="small" themeColor="textSecondary">
            고른 분의 기록을 참고해서 답하고, 대화도 분마다 따로 보관돼요.
          </ThemedText>
          <View style={styles.options}>
            {options.map((o) => {
              const isSelected = o.id === selected;
              return (
                <Pressable
                  key={o.id ?? 'none'}
                  accessibilityRole="radio"
                  accessibilityState={{ checked: isSelected }}
                  accessibilityLabel={`${o.label}, ${o.caption}`}
                  onPress={() => onSelect(o.id)}
                  style={({ pressed }) => [
                    styles.option,
                    { borderTopColor: theme.border },
                    pressed && { backgroundColor: theme.backgroundSelected },
                  ]}>
                  {/* 고른 줄은 먹색 네모 + ✓ (오늘의 돌봄 체크 상자와 같은 모양) */}
                  <View
                    style={[
                      styles.mark,
                      { borderColor: theme.rule, backgroundColor: isSelected ? theme.primary : 'transparent' },
                    ]}>
                    {isSelected ? (
                      <ThemedText type="smallBold" style={{ color: theme.onPrimary }}>
                        ✓
                      </ThemedText>
                    ) : null}
                  </View>
                  <View style={styles.flex}>
                    <ThemedText type="bold">{o.label}</ThemedText>
                    <ThemedText type="small" themeColor="textSecondary">
                      {o.caption}
                    </ThemedText>
                  </View>
                </Pressable>
              );
            })}
          </View>
        </View>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  button: {
    minHeight: 36,
    maxWidth: 160,
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.one,
    // 테두리 없이 글자만 (머리글 제목 줄에 자연스럽게). 누르는 영역은 높이 36 + hitSlop으로 유지
    borderRadius: Radius.card,
    paddingHorizontal: Spacing.two,
    marginRight: Spacing.three,
  },
  buttonLabel: {
    flexShrink: 1,
  },
  backdropWrap: {
    flex: 1,
    justifyContent: 'flex-end',
  },
  backdrop: {
    backgroundColor: 'rgba(0,0,0,0.4)',
  },
  sheet: {
    borderTopWidth: 2,
    padding: Spacing.four,
    gap: Spacing.two,
  },
  options: {
    marginTop: Spacing.two,
  },
  option: {
    minHeight: MinTouch + Spacing.three,
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.three,
    borderTopWidth: StyleSheet.hairlineWidth,
    paddingVertical: Spacing.two,
  },
  mark: {
    width: 24,
    height: 24,
    borderWidth: 2,
    alignItems: 'center',
    justifyContent: 'center',
  },
  flex: {
    flex: 1,
  },
});
