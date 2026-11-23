import tseslint from "typescript-eslint";

export default [
  {
    files: ["**/*.ts", "**/*.tsx"],
    languageOptions: {
      parser: tseslint.parser,
      parserOptions: { ecmaVersion: "latest", sourceType: "module", ecmaFeatures: { jsx: true } },
      globals: { window: "readonly", document: "readonly", fetch: "readonly", console: "readonly", URL: "readonly", FormData: "readonly", EventSource: "readonly", setTimeout: "readonly", clearTimeout: "readonly", React: "readonly" },
    },
    rules: {
      "no-debugger": "error",
      "no-duplicate-case": "error",
      "no-constant-binary-expression": "error",
      "no-unsafe-finally": "error",
      "no-var": "error",
      "prefer-const": "error",
      eqeqeq: ["error", "always"],
    },
  },
];
