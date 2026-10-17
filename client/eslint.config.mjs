// ESLint flat config (ESLint 9+/10). Replaces the legacy .eslintrc.json —
// ESLint dropped .eslintrc support and the `--ext` CLI flag, so file
// extensions are selected via the `files` patterns below.
import js from '@eslint/js'
import tseslint from '@typescript-eslint/eslint-plugin'
import tsparser from '@typescript-eslint/parser'
import reactHooks from 'eslint-plugin-react-hooks'
import globals from 'globals'

// Local rule: forbid creating a `signal()` / `computed()` inside a component
// or hook body. A fresh signal is minted on every render, so writing it
// triggers a re-render that recreates it at its initial value — local state
// silently resets (the toggles/loading-state bug fixed across the SPA). Use
// `useSignal()` / `useComputed()` (stable across renders) instead. Module-level
// `signal()` (shared/store state) is fine and not flagged.
const localPlugin = {
  rules: {
    'no-signal-in-component': {
      meta: {
        type: 'problem',
        docs: { description: 'Disallow signal()/computed() in a component/hook body; use useSignal()/useComputed().' },
        messages: {
          inComponent: "`{{name}}()` in a component/hook body creates a NEW signal every render, so its state resets on re-render. Use `use{{Cap}}()` instead.",
        },
        schema: [],
      },
      create(context) {
        // Name of the nearest enclosing component (PascalCase) or hook (useX)
        // function, walking the lexical parent chain. null if none (e.g. a
        // module-level call or a plain lowercase helper).
        function enclosingComponentOrHook(node) {
          for (let n = node.parent; n; n = n.parent) {
            if (
              n.type === 'FunctionDeclaration' ||
              n.type === 'FunctionExpression' ||
              n.type === 'ArrowFunctionExpression'
            ) {
              const name =
                (n.id && n.id.name) ||
                (n.parent && n.parent.type === 'VariableDeclarator' && n.parent.id && n.parent.id.name) ||
                null
              if (name && (/^[A-Z]/.test(name) || /^use[A-Z]/.test(name))) return true
            }
          }
          return false
        }
        return {
          CallExpression(node) {
            const callee = node.callee
            if (
              callee.type === 'Identifier' &&
              (callee.name === 'signal' || callee.name === 'computed') &&
              enclosingComponentOrHook(node)
            ) {
              context.report({
                node,
                messageId: 'inComponent',
                data: { name: callee.name, Cap: callee.name === 'signal' ? 'Signal' : 'Computed' },
              })
            }
          },
        }
      },
    },
  },
}

export default [
  {
    ignores: [
      'dist/**',
      'node_modules/**',
      'coverage/**',
      '**/*.config.ts',
      '**/*.config.js',
      '**/*.config.mjs',
    ],
  },
  js.configs.recommended,
  {
    // Preserve the project's prior lint contract across the ESLint 8→10
    // migration: the legacy .eslintrc never reported unused-disable
    // directives, and `no-useless-assignment` (new in eslint:recommended)
    // false-positives on init-then-conditionally-reassign + try/catch
    // patterns already in the tree. Enabling either is a separate cleanup,
    // not part of the dependency bump.
    linterOptions: { reportUnusedDisableDirectives: 'off' },
    rules: { 'no-useless-assignment': 'off' },
  },
  {
    files: ['**/*.ts', '**/*.tsx'],
    languageOptions: {
      parser: tsparser,
      ecmaVersion: 2022,
      sourceType: 'module',
      parserOptions: { ecmaFeatures: { jsx: true } },
      globals: { ...globals.browser, ...globals.node },
    },
    plugins: {
      '@typescript-eslint': tseslint,
      'react-hooks': reactHooks,
      local: localPlugin,
    },
    rules: {
      ...tseslint.configs.recommended.rules,
      // Preact hooks follow React's rules. `rules-of-hooks` is high-signal
      // (conditional/looped hook calls are real bugs) — error. `exhaustive-deps`
      // is valuable but flags a long tail of existing effects, so it rides as a
      // warning (doesn't fail CI) until a dedicated cleanup turns it to error.
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
      // Guard the render-body signal() footgun (state-loss bug). See above.
      'local/no-signal-in-component': 'error',
      // Importing preact/compat patches Preact's `options.vnode` for the whole
      // app the moment its chunk loads: `onChange` on text inputs becomes
      // `oninput`, `onFocus`/`onBlur` bubble, numeric styles gain `px`. The SPA
      // is core-only (hooks from preact/hooks, `createPortal` from preact).
      'no-restricted-imports': ['error', {
        paths: ['preact/compat', 'react', 'react-dom'].map((name) => ({
          name,
          message: 'preact/compat changes event and style handling app-wide once loaded; use preact, preact/hooks or a core-only helper instead.',
        })),
      }],
      'no-unused-vars': 'off',
      '@typescript-eslint/no-unused-vars': [
        'error',
        {
          argsIgnorePattern: '^_',
          varsIgnorePattern: '^_',
          caughtErrorsIgnorePattern: '^_',
          destructuredArrayIgnorePattern: '^_',
        },
      ],
      '@typescript-eslint/no-explicit-any': 'off',
      '@typescript-eslint/no-non-null-assertion': 'off',
      '@typescript-eslint/ban-ts-comment': [
        'error',
        { 'ts-ignore': 'allow-with-description', 'ts-expect-error': 'allow-with-description' },
      ],
      'no-empty': ['error', { allowEmptyCatch: true }],
      'no-constant-condition': ['error', { checkLoops: false }],
      'no-undef': 'off',
      'prefer-const': 'error',
      'no-var': 'error',
      eqeqeq: ['error', 'always', { null: 'ignore' }],
    },
  },
  {
    // Test files run under Vitest globals.
    files: ['**/*.test.ts', '**/*.test.tsx'],
    languageOptions: {
      globals: {
        describe: 'readonly',
        it: 'readonly',
        expect: 'readonly',
        vi: 'readonly',
        beforeAll: 'readonly',
        beforeEach: 'readonly',
        afterAll: 'readonly',
        afterEach: 'readonly',
      },
    },
    rules: {
      // Tests legitimately create throwaway signals in helper closures.
      'local/no-signal-in-component': 'off',
    },
  },
]
