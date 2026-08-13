/** Query result table component with pagination. */

import React, { useState } from "react";
import { Table, Tag, Button, Dropdown } from "antd";
import { DownloadOutlined } from "@ant-design/icons";
import { QueryResult, ExportFormat } from "../types/query";

interface ResultTableProps {
  result: QueryResult | null;
  loading?: boolean;
  /** Called when the user picks an export format. */
  onExport?: (format: ExportFormat) => void;
}

export const ResultTable: React.FC<ResultTableProps> = ({
  result,
  loading = false,
  onExport,
}) => {
  const [pagination, setPagination] = useState({
    current: 1,
    pageSize: 50,
  });

  if (!result) {
    return null;
  }

  const columns = result.columns.map((col) => ({
    title: col.name,
    dataIndex: col.name,
    key: col.name,
    render: (value: any) => {
      if (value === null || value === undefined) {
        return <Tag color="default">NULL</Tag>;
      }
      if (typeof value === "boolean") {
        return value ? "✓" : "✗";
      }
      if (value instanceof Date) {
        return value.toLocaleString();
      }
      return String(value);
    },
  }));

  const handleTableChange = (newPagination: any) => {
    setPagination({
      current: newPagination.current,
      pageSize: newPagination.pageSize,
    });
  };

  const exportItems = [
    { key: "csv", label: "Export as CSV" },
    { key: "json", label: "Export as JSON" },
    { key: "ndjson", label: "Export as NDJSON" },
  ];

  return (
    <div>
      <div style={{ marginBottom: 16, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div>
          <Tag color="blue">Rows: {result.rowCount}</Tag>
          <Tag color="green">Execution Time: {result.executionTimeMs}ms</Tag>
        </div>
        {onExport && (
          <Dropdown
            menu={{
              items: exportItems,
              onClick: ({ key }) => onExport(key as ExportFormat),
            }}
            trigger={["click"]}
          >
            <Button
              size="small"
              icon={<DownloadOutlined />}
              disabled={result.rowCount === 0}
            >
              Export
            </Button>
          </Dropdown>
        )}
      </div>
      <Table
        columns={columns}
        dataSource={result.rows.map((row, index) => ({
          ...row,
          key: index,
        }))}
        loading={loading}
        pagination={{
          current: pagination.current,
          pageSize: pagination.pageSize,
          total: result.rowCount,
          showSizeChanger: true,
          showTotal: (total) => `Total ${total} rows`,
          pageSizeOptions: ["10", "50", "100", "500"],
        }}
        onChange={handleTableChange}
        scroll={{ x: "max-content" }}
      />
    </div>
  );
};
