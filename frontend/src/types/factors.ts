export interface FactorDefinition {
  factor_id: string
  name: string
  expression: string
  description: string
  category: string
  source: 'alpha158' | 'custom'
  created_at: string
  read_only: boolean
}

export interface FactorTemplate {
  name: string
  expression: string
  description: string
}

export interface FactorCatalog {
  items: FactorDefinition[]
  total: number
  system_count: number
  custom_count: number
  storage: 'postgresql'
  templates: FactorTemplate[]
  syntax: {
    fields: string[]
    functions: Array<{ signature: string; description: string }>
    max_window: number
    max_length: number
  }
  limits: { preview_stocks: number; preview_rows: number }
}

export interface FactorValue { code: string; date: string; value: number | null }

export interface FactorValues {
  factor_id: string
  items: FactorValue[]
  total: number
  available_values: number
  as_of: string | null
  source: 'stored' | 'computed'
  status: 'ready' | 'empty' | 'insufficient_history'
  message: string
}

export interface FactorDraft { name: string; expression: string }

export interface FactorValidation extends FactorDraft {
  valid: true
  fields: string[]
  lookback_rows: number
  preview: FactorValues
}

export interface FactorCreateRequest extends FactorDraft {
  confirmed: true
  request_id: string
  idempotency_key: string
}
