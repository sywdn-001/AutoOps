import { TestBrowser } from '@@/testBrowser';
import { render } from '@testing-library/react';
import React from 'react';

/**
 * 堡垒机登录页渲染用例。
 *
 * 这里只验证「页面能渲染出堡垒机登录表单」——真正的登录链路（JWT 下发、
 * 失败锁定、审计落库）由后端 pytest 覆盖（bastion-backend/tests/test_api_auth.py），
 * 前端不再依赖 Ant Design Pro 自带的录制回放 mock server。
 */
describe('Login Page', () => {
  it('渲染堡垒机登录表单', async () => {
    const historyRef = React.createRef<any>();
    const rootContainer = render(
      <TestBrowser
        historyRef={historyRef}
        location={{
          pathname: '/user/login',
        }}
      />,
    );

    expect(await rootContainer.findByText('AutoOps 堡垒机')).toBeTruthy();
    expect(
      await rootContainer.findByPlaceholderText('堡垒机账号'),
    ).toBeTruthy();
    expect(await rootContainer.findByPlaceholderText('登录密码')).toBeTruthy();

    rootContainer.unmount();
  });
});
