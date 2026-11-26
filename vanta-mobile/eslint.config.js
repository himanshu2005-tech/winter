// https://docs.expo.dev/guides/using-eslint/
const { defineConfig } = require('eslint/config');
const expoConfig = require('eslint-config-expo/flat');

module.exports = defineConfig([
  expoConfig,
  {
    ignores: [
      'dist/*',
      // Unmodified create-expo-app examples retained only as upstream
      // reference; no production route imports them.
      'src/hooks/*',
      'src/constants/*',
      'src/components/animated-icon*',
      'src/components/app-tabs*',
      'src/components/external-link.tsx',
      'src/components/hint-row.tsx',
      'src/components/themed-*.tsx',
      'src/components/ui/*',
      'src/components/web-badge.tsx',
    ],
  },
]);
