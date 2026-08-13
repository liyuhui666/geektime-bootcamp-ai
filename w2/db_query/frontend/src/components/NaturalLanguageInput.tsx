/** Natural language query input component. */

import React, { useState } from "react";
import { Input, Button, Space, Typography, Alert } from "antd";
import { SendOutlined, LoadingOutlined } from "@ant-design/icons";

const { TextArea } = Input;
const { Text } = Typography;

interface NaturalLanguageInputProps {
  onGenerateSQL: (prompt: string) => void;
  loading?: boolean;
  error?: string | null;
}

export const NaturalLanguageInput: React.FC<NaturalLanguageInputProps> = ({
  onGenerateSQL,
  loading = false,
  error = null,
}) => {
  const [prompt, setPrompt] = useState("");

  const handleSubmit = () => {
    if (prompt.trim()) {
      onGenerateSQL(prompt.trim());
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    // Submit on Cmd/Ctrl + Enter
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      handleSubmit();
    }
  };

  return (
    <Space direction="vertical" style={{ width: "100%" }} size={12}>
      <div>
        <Text strong style={{ fontSize: 13, textTransform: "uppercase" }}>
          Ask a question, paste SQL, or export
        </Text>
        <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
          (English or Chinese)
        </Text>
      </div>

      <TextArea
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={"例如：查询所有未完成的任务\n或直接贴 SQL + 导出指令：\nSELECT * FROM `bigdata`.`mysql_flink1` LIMIT 1000; 导出数据文件为csv格式"}
        rows={4}
        style={{
          fontSize: 15,
          borderWidth: 2,
          borderRadius: 2,
        }}
        disabled={loading}
      />

      {error && (
        <Alert
          message="Generation Failed"
          description={error}
          type="error"
          closable
          style={{ borderWidth: 2 }}
        />
      )}

      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          Press Cmd/Ctrl + Enter to run · 末尾加「导出为 csv/json/ndjson」可直接出文件
        </Text>
        <Button
          type="primary"
          icon={loading ? <LoadingOutlined /> : <SendOutlined />}
          onClick={handleSubmit}
          loading={loading}
          disabled={!prompt.trim() || loading}
          size="large"
          style={{
            height: 40,
            paddingLeft: 20,
            paddingRight: 20,
            fontWeight: 700,
          }}
        >
          RUN
        </Button>
      </div>
    </Space>
  );
};
