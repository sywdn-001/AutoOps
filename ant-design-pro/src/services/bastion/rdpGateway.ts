/**
 * RDP 网关（mstsc 连 4489）的前端接口：状态查询、启停、以及两个设置项。
 *
 * 网关本身是独立进程（`bastion-rdp-gateway`，Rust），这里只调后端的管理接口；
 * 页面在「系统设置 → RDP 网关」。
 */
import { request } from '@umijs/max';

export type RdpGatewayStatus = {
  enabled: boolean;
  port: number;
  listening: boolean;
  pid?: number | null;
  binary: string;
  binaryExists: boolean;
  logFile: string;
};

export const rdpGatewayApi = {
  status: async (): Promise<RdpGatewayStatus> =>
    request<RdpGatewayStatus>('/api/rdp-gateway/status', { method: 'GET' }),
  start: async (): Promise<{ started: boolean; pid?: number; port: number }> =>
    request('/api/rdp-gateway/start', { method: 'POST' }),
  stop: async (): Promise<{ stopped: boolean; pid?: number }> =>
    request('/api/rdp-gateway/stop', { method: 'POST' }),
};
