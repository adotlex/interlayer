/**
 * U1 — Registry & providers. The unit's public face.
 *
 * Exports only what belongs to this unit. The library barrel (`src/index.ts`)
 * is written by the orchestrator after the wave; nothing here re-exports core,
 * another unit, or the test harness.
 */

export type { CapabilityMeta } from './capability.ts';
export {
  capability,
  capabilityMeta,
  capabilityNames,
  declaresCapability,
  defineContract,
} from './capability.ts';
export type {
  HealthProbe,
  ProviderDefinition,
  ProviderLike,
  ResolvedRegistration,
} from './provider.ts';
export { defineProvider, implementedCapabilities, supports, toProviderRecord } from './provider.ts';
export type { RegistryOptions } from './registry.ts';
export { createRegistry } from './registry.ts';
