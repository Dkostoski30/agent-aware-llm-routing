import designTokens from '../../../../docs/design-system/tokens.json';

export type ThemeName = 'dark' | 'light';

// A colour whose value names another colour token, e.g. avatar = "{accent}".
const REFERENCE = /^\{(.+)\}$/;

/** Every design token as a CSS custom property (--bg, --space-4, ...), colours and shadows for one theme. */
export function cssVariables(theme: ThemeName): Record<string, string> {
  const colours = new Map(
    designTokens.color.tokens.map((token) => [token.name, token.value[theme]]),
  );
  const resolve = (value: string): string => {
    const reference = REFERENCE.exec(value);
    if (!reference) return value;
    const target = colours.get(reference[1]);
    if (target === undefined) throw new Error(`tokens.json: unknown colour token ${reference[1]}`);
    return resolve(target);
  };

  const variables: Record<string, string> = {};
  for (const token of designTokens.color.tokens) {
    variables[`--${token.name}`] = resolve(token.value[theme]);
  }
  for (const token of designTokens.shadow.tokens) {
    variables[`--${token.name}`] = token.value[theme];
  }
  for (const [family, stack] of Object.entries(designTokens.type.families)) {
    variables[`--font-${family}`] = stack;
  }
  for (const group of [
    designTokens.spacing,
    designTokens.radius,
    designTokens.layout,
    designTokens.size,
  ]) {
    for (const token of group.tokens) variables[`--${token.name}`] = token.value;
  }
  return variables;
}
