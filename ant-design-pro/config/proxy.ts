/**
 * @name 代理的配置
 * @see 在生产环境 代理是无法生效的，所以这里没有生产环境的配置
 * -------------------------------
 * The agent cannot take effect in the production environment
 * so there is no configuration of the production environment
 * For details, please see
 * https://pro.ant.design/docs/deploy
 *
 * @doc https://umijs.org/docs/guides/proxy
 */
export default {
  /**
   * @name 堡垒机后端（Flask）本地开发代理
   * 前端 `npm run dev`（MOCK=none）时，/api/** 与 /socket.io/** 全部转发到 Flask 后端。
   * 端口与 `python run.py --port 5000` 保持一致，可用 BASTION_API 环境变量覆盖。
   */
  dev: {
    '/api/': {
      target: process.env.BASTION_API || 'http://127.0.0.1:5000',
      changeOrigin: true,
    },
    // 网页终端的 Socket.IO 长连接（websocket 需要 ws: true）
    '/socket.io/': {
      target: process.env.BASTION_API || 'http://127.0.0.1:5000',
      changeOrigin: true,
      ws: true,
    },
  },
  /**
   * @name 详细的代理配置
   * @doc https://github.com/chimurai/http-proxy-middleware
   */
  test: {
    // localhost:8000/api/** -> https://pro-api.ant-design-demo.workers.dev/api/**
    '/api/': {
      target: 'https://pro-api.ant-design-demo.workers.dev',
      changeOrigin: true,
    },
  },
  pre: {
    '/api/': {
      target: 'your pre url',
      changeOrigin: true,
    },
  },
};
