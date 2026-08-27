import { describe, expect, it } from 'vitest';
import { serializeExecutionAt } from '../investmentPlans';

describe('serializeExecutionAt', () => {
  it('preserves browser wall time with the historical UTC offset', () => {
    expect(serializeExecutionAt('2026-08-27T10:00:00', 240)).toBe(
      '2026-08-27T10:00:00-04:00',
    );
    expect(serializeExecutionAt('2026-08-27T22:00:00', -480)).toBe(
      '2026-08-27T22:00:00+08:00',
    );
    expect(serializeExecutionAt('2026-08-27T10:00', 240)).toBe(
      '2026-08-27T10:00:00-04:00',
    );
  });

  it('rejects date-only values', () => {
    expect(() => serializeExecutionAt('2026-08-27', -480)).toThrow(/seconds/);
  });
});
