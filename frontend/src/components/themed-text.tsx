import { Children, type ReactNode } from 'react';
import { StyleSheet, Text, type TextProps } from 'react-native';

import { FontFamily, FontSize, ThemeColor } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';

export type ThemedTextProps = TextProps & {
  type?: 'default' | 'bold' | 'title' | 'subtitle' | 'small' | 'smallBold' | 'link';
  themeColor?: ThemeColor;
};

// 한글 바로 앞뒤에 넣는 WORD JOINER(U+2060, 폭 0). 이 문자 사이에서는 줄을 바꾸지 않으므로
// 한글은 띄어쓰기에서만 줄이 바뀐다 ("경도인지\n장애" → "경도인지장애\n환자들은").
// 안드로이드에는 한국어 어절 단위 줄바꿈 옵션이 없어서 모든 플랫폼에 같은 방식을 쓴다.
const HANGUL_PAIR = /([가-힣])(?=\S)|(\S)(?=[가-힣])/gu;

function keepKoreanWords(children: ReactNode): ReactNode {
  return Children.map(children, (child) =>
    typeof child === 'string' ? child.replace(HANGUL_PAIR, '$1$2⁠') : child,
  );
}

export function ThemedText({ style, type = 'default', themeColor, children, ...rest }: ThemedTextProps) {
  const theme = useTheme();

  return (
    <Text
      style={[
        { color: theme[themeColor ?? (type === 'link' ? 'primary' : 'text')] },
        styles[type],
        style,
      ]}
      lineBreakStrategyIOS="hangul-word"
      {...rest}>
      {keepKoreanWords(children)}
    </Text>
  );
}

const styles = StyleSheet.create({
  default: {
    fontSize: FontSize.body,
    lineHeight: 28,
    fontFamily: FontFamily.regular,
  },
  bold: {
    fontSize: FontSize.body,
    lineHeight: 28,
    fontFamily: FontFamily.bold,
  },
  title: {
    fontSize: FontSize.title,
    lineHeight: 36,
    fontFamily: FontFamily.bold,
  },
  subtitle: {
    fontSize: FontSize.subtitle,
    lineHeight: 30,
    fontFamily: FontFamily.bold,
  },
  small: {
    fontSize: FontSize.small,
    lineHeight: 22,
    fontFamily: FontFamily.regular,
  },
  smallBold: {
    fontSize: FontSize.small,
    lineHeight: 22,
    fontFamily: FontFamily.semibold,
  },
  link: {
    fontSize: FontSize.body,
    lineHeight: 28,
    fontFamily: FontFamily.bold,
  },
});
