export interface TableColumnMeta {
  key: string
  title: string
  data_type: 'string' | 'number' | 'boolean'
}

export interface TablePayload<T extends object = Record<string, unknown>> {
  columns: TableColumnMeta[]
  records: T[]
  total: number
  offset?: number
  limit?: number
}
