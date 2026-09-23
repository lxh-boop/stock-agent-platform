export const queryKeys = {
  health: ['health'] as const,
  paperTrading: (userId: string) => ['web', 'paper-trading', userId] as const,
  paperHistory: (userId: string, tradeDate: string) => ['web', 'paper-trading', 'history', userId, tradeDate] as const,
  paperProfile: (userId: string) => ['web', 'paper-trading', 'profile', userId] as const,
  paperProposals: (userId: string) => ['web', 'paper-trading', 'proposals', userId] as const,
}
