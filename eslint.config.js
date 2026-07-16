// Flat ESLint config for the SPA under web/src.
//
// Non-type-checked typescript-eslint (no project service) so it stays fast.
// The @ts-nocheck migration is done and noImplicitAny is on (see tsconfig.json),
// so the only remaining `any` is the api<T = any> default plus a few loose
// payload spots; no-explicit-any stays off until those are pinned. Genuinely
// dead code (unused vars/imports) is an error so cleanups don't silently regress.
import js from "@eslint/js";
import tseslint from "typescript-eslint";

export default tseslint.config(
  {
    ignores: ["web/dist/**", "node_modules/**", "*.config.{js,ts}"],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["web/src/**/*.ts"],
    languageOptions: {
      globals: {
        window: "readonly",
        document: "readonly",
        location: "readonly",
        localStorage: "readonly",
        fetch: "readonly",
        setTimeout: "readonly",
        clearTimeout: "readonly",
        history: "readonly",
        URLSearchParams: "readonly",
        navigator: "readonly",
        console: "readonly",
        HTMLElement: "readonly",
        Element: "readonly",
        Event: "readonly",
        CustomEvent: "readonly",
      },
    },
    rules: {
      // A handful of loose payload spots + the api<T = any> default still use
      // `any`; keep this off until those are pinned to api-types DTOs. ban-ts-
      // comment stays off as the cheap escape hatch (no directives in tree today).
      "@typescript-eslint/no-explicit-any": "off",
      "@typescript-eslint/ban-ts-comment": "off",
      // Allow leading-underscore throwaways (e.g. `catch (_e)`); flag the rest.
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_", caughtErrorsIgnorePattern: "^_" },
      ],
    },
  },
  {
    files: ["web/tests/**/*.ts", "e2e/**/*.ts"],
    rules: {
      // Test fixtures intentionally cast partial API payloads; production source
      // keeps its own stricter boundary while tests still get dead-code checks.
      "@typescript-eslint/no-explicit-any": "off",
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_", caughtErrorsIgnorePattern: "^_" },
      ],
    },
  },
);
