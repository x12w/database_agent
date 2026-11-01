import { apiGet, apiSend } from './client.ts'
export type SettingValue = string | number | boolean
export interface SettingField {
  key: string; label: string; group: string
  type: 'text' | 'secret' | 'number' | 'boolean' | 'select'
  min: number | null; max: number | null; step: number
  choices?: { value: string; label: string }[]
}
export interface Preferences {
  values: Record<string, SettingValue>
  secrets: Record<string, boolean>
  fields: SettingField[]
  groups: Record<string, string>
  revision: string
  desktop: boolean
  data_dir: string
  config_file: string
  sandbox_available: boolean
}
export const fetchSettings = () => apiGet<Preferences>('/api/admin/settings')
export const saveSettings = (values: Record<string, SettingValue>, revision: string) =>
  apiSend<{ revision: string }>('PUT', '/api/admin/settings', { values, revision })
export const testModel = (values: Record<string, SettingValue>) =>
  apiSend<{ message: string; elapsed_ms: number }>('POST', '/api/admin/settings/test-model', { values })
