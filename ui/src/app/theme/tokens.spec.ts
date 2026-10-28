import designTokens from '../../../../docs/design-system/tokens.json';
import { cssVariables } from './tokens';

function colour(name: string) {
  const token = designTokens.color.tokens.find((t) => t.name === name);
  if (!token) throw new Error(`no colour token ${name}`);
  return token.value;
}

describe('cssVariables', () => {
  it('takes each colour from the chosen theme', () => {
    expect(cssVariables('dark')['--bg']).toBe(colour('bg').dark);
    expect(cssVariables('light')['--bg']).toBe(colour('bg').light);
  });

  it('resolves a colour that names another token', () => {
    expect(colour('avatar').dark).toBe('{accent}');
    expect(cssVariables('dark')['--avatar']).toBe(colour('accent').dark);
    expect(cssVariables('light')['--avatar']).toBe(colour('accent').light);
  });

  it('takes each shadow from the chosen theme', () => {
    const raise = designTokens.shadow.tokens.find((t) => t.name === 'shadow-raise');
    expect(cssVariables('light')['--shadow-raise']).toBe(raise?.value.light);
  });

  it('keeps fonts, spacing, radius, layout and sizes the same in both themes', () => {
    for (const theme of ['dark', 'light'] as const) {
      const vars = cssVariables(theme);
      expect(vars['--font-sans']).toBe(designTokens.type.families.sans);
      expect(vars['--font-mono']).toBe(designTokens.type.families.mono);
      expect(vars['--space-4']).toBe('16px');
      expect(vars['--radius-lg']).toBe('14px');
      expect(vars['--panel-width']).toBe('400px');
      expect(vars['--horse-4x']).toBe('56px');
    }
  });
});
