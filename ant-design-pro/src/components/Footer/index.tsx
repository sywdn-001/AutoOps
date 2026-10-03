import { createStyles } from 'antd-style';
import React from 'react';

const useStyles = createStyles(({ token, css }) => ({
  footer: css`
    padding: 16px 24px;
    text-align: center;
    color: ${token.colorTextDescription};
    font-size: ${token.fontSizeSM}px;
    line-height: ${token.lineHeight};
    background: transparent;
  `,
  brand: css`
    margin-bottom: 4px;
  `,
  tagline: css`
    color: ${token.colorTextQuaternary};
  `,
}));

const Footer: React.FC = () => {
  const { styles } = useStyles();
  const year = new Date().getFullYear();

  return (
    <div className={styles.footer}>
      <div className={styles.brand}>AutoOps 堡垒机 &copy; {year}</div>
      <div className={styles.tagline}>命令级策略管控 · 会话全程留痕</div>
    </div>
  );
};

export default Footer;
