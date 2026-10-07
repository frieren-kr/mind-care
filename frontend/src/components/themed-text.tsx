import { StyleSheet, Text, type TextProps } from 'react-native';

import { FontSize, ThemeColor } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';

export type ThemedTextProps = TextProps & {
  type?: 'default' | 'bold' | 'title' | 'subtitle' | 'small' | 'smallBold' | 'link';
  themeColor?: ThemeColor;
};

export function ThemedText({ style, type = 'default', themeColor, ...rest }: ThemedTextProps) {
  const theme = useTheme();

  return (
    <Text
      style={[
        { color: theme[themeColor ?? (type === 'link' ? 'primary' : 'text')] },
        styles[type],
        style,
      ]}
      {...rest}
    />
  );
}

const styles = StyleSheet.create({
  default: {
    fontSize: FontSize.body,
    lineHeight: 28,
    fontWeight: 400,
  },
  bold: {
    fontSize: FontSize.body,
    lineHeight: 28,
    fontWeight: 700,
  },
  title: {
    fontSize: FontSize.title,
    lineHeight: 36,
    fontWeight: 700,
  },
  subtitle: {
    fontSize: FontSize.subtitle,
    lineHeight: 30,
    fontWeight: 700,
  },
  small: {
    fontSize: FontSize.small,
    lineHeight: 22,
    fontWeight: 400,
  },
  smallBold: {
    fontSize: FontSize.small,
    lineHeight: 22,
    fontWeight: 700,
  },
  link: {
    fontSize: FontSize.body,
    lineHeight: 28,
    fontWeight: 700,
  },
});
