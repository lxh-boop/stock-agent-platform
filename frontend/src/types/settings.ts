export interface CredentialStatus {
  tushare_configured: boolean
  llm_configured: boolean
}

export interface PublicLlmStatus {
  mode: 'api' | 'local' | string
  provider: string
  model: string
  endpoint_configured: boolean
  profile_id: string
}

export interface EditableSettingsConfiguration {
  llm_mode: 'api' | 'local'
  api: {
    provider: string
    base_url: string
    model: string
    configured: boolean
    custom_configured: boolean
    default_available: boolean
  }
  local: {
    provider: string
    base_url: string
    effective_base_url: string
    model: string
  }
  tushare: {
    configured: boolean
    custom_configured: boolean
    default_available: boolean
  }
}


export interface SchedulerSettings {
  enabled: boolean
  hour: number
  minute: number
  timezone: string
  catch_up: boolean
  runtime_running: boolean
  job_registered: boolean
  next_run_time: string
  expected_signal_date: string
  latest_signal_date: string
  stale: boolean
  last_started_at: string
  last_finished_at: string
  last_trade_date: string
  last_status: string
  current_step: string
  last_error: string
  last_failed_step?: string
  last_error_type?: string
}

export interface PublicSettings {
  universe: string
  model_backend: string
  model_version: string
  default_topk: number
  current_user_id: string
  feature_flags: Record<string, boolean>
  credentials: CredentialStatus
  llm: PublicLlmStatus
  configuration: EditableSettingsConfiguration
  scheduler: SchedulerSettings
  read_only: boolean
}

export interface SettingsUpdateRequest {
  request_id: string
  idempotency_key: string
  confirmed: boolean
  llm_mode: 'api' | 'local'
  api_provider: string
  api_base_url: string
  api_model: string
  api_credential?: string
  clear_api_credential: boolean
  local_base_url: string
  local_model: string
  tushare_credential?: string
  clear_tushare_credential: boolean
  scheduler_enabled: boolean
  scheduler_hour: number
  scheduler_minute: number
  scheduler_catch_up: boolean
}

export interface SettingsUpdateResult {
  request_id: string
  idempotency_key: string
  status: string
  settings: PublicSettings
}
