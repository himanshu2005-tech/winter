export type VantaEvent = {
  id?: number;
  at?: string;
  kind: string;
  payload: unknown;
};

export type ConnectionStatus = 'offline' | 'connecting' | 'online';

export type GatewayStatus = {
  online: boolean;
  mode: string;
  standby: boolean;
  connected_clients: number;
};

export type Pairing = {
  baseUrl: string;
  token: string;
  expiresAt?: string;
};
