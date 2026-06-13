// Flat ESLint config for the SPA under web/src.
//
// Non-type-checked typescript-eslint (no project service) so it stays fast and
// works regardless of @ts-nocheck markers. Tightens as modules are typed: real
// `any` use is allowed for now (the migration leans on it), but genuinely dead
// code (unused vars/imports) is an error so cleanups like dropping orphaned
// imports don't silently regress.
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
      // The TS migration intentionally leans on `any` and ts-directives while
      // modules are still @ts-nocheck; don't fail the build on those yet.
      "@typescript-eslint/no-explicit-any": "off",
      "@typescript-eslint/ban-ts-comment": "off",
      // Allow leading-underscore throwaways (e.g. `catch (_e)`); flag the rest.
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_", caughtErrorsIgnorePattern: "^_" },
      ],
    },
  },
);
