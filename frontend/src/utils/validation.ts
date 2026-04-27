export interface ValidationResult {
  valid: boolean;
  errorMessage?: string;
}

const NAME_PREFIX_PATTERN = /^[a-zA-Z0-9-]{1,32}$/;

/**
 * Validate an optional name-prefix string for replicated resource names.
 * An empty string is considered valid (no prefix). Non-empty values must
 * match /^[a-zA-Z0-9-]{1,32}$/ — alphanumerics and hyphens only, up to 32
 * chars — because AWS resource names require a safe character subset.
 */
export function validateNamePrefix(value: string): ValidationResult {
  if (value === "") {
    return { valid: true };
  }
  if (value.length > 32) {
    return {
      valid: false,
      errorMessage: "Name prefix must be 32 characters or fewer.",
    };
  }
  if (!NAME_PREFIX_PATTERN.test(value)) {
    return {
      valid: false,
      errorMessage:
        "Name prefix may contain letters, digits, and hyphens only.",
    };
  }
  return { valid: true };
}
