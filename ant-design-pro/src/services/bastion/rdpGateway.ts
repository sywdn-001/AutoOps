/**
 * RDP 网关（mstsc 连 4489）的前端接口：状态查询、启停、诊断。
 *
 * 注意：本项目的 `request` 返回的是**统一信封**（`{success, data}`），
 * 必须用 `unwrap()` 解包 —— 否则拿到的是信封本身（`status.listening` 是 undefined、
 * `diagnose.candidates.map` 直接抛错，页面就崩成「页面出现错误」）。
 */
import { request } from '@umijs/max';
import { unwrap } from '@/services/bastion/client';
import type { ApiData } from '@/services/bastion/types';

export type RdpGatewayStatus = {
  enabled: boolean;
  port: number;
  listening: boolean;
  pid?: number | null;
  binary: string;
  binaryExists: boolean;
  logFile: string;
};

export type RdpGatewayDiagnose = {
  apiVersion: number;
  moduleFile: string;
  cwd: string;
  instancePath: string;
  binary: string;
  binaryExists: boolean;
  candidates?: { path: string; exists: boolean }[];
  pidFile: string;
  port: number;
  listening: boolean;
  pid?: number | null;
  enabledSetting: string;
};

export const rdpGatewayApi = {
  status: (): Promise<RdpGatewayStatus> =>
    unwrap(request<ApiData<RdpGatewayStatus>>('/api/rdp-gateway/status')),
  start: (): Promise<{ started: boolean; pid?: number; port: number }> =>
    unwrap(request('/api/rdp-gateway/start', { method: 'POST' })),
  stop: (): Promise<{ stopped: boolean; pid?: number }> =>
    unwrap(request('/api/rdp-gateway/stop', { method: 'POST' })),
  diagnose: (): Promise<RdpGatewayDiagnose> =>
    unwrap(request<ApiData<RdpGatewayDiagnose>>('/api/rdp-gateway/diagnose')),
};
