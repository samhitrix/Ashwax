import {
  useReactTable,
  getCoreRowModel,
  flexRender,
  createColumnHelper,
} from '@tanstack/react-table'
import { useDashboard } from '../store/dashboard'
import type { Order } from '../types'

const STATUS_COLOR: Record<string, string> = {
  FILLED: 'text-green-400',
  OPEN: 'text-blue-400',
  PENDING: 'text-yellow-400',
  PARTIAL: 'text-orange-400',
  CANCELLED: 'text-slate-500',
  REJECTED: 'text-red-400',
}

const col = createColumnHelper<Order>()
const COLUMNS = [
  col.accessor('orderId',   { header: 'Order ID',  cell: (i) => <span className="text-slate-400">{i.getValue()}</span> }),
  col.accessor('symbol',    { header: 'Symbol',    cell: (i) => <span className="text-white font-medium">{i.getValue().split(':').pop()}</span> }),
  col.accessor('direction', { header: 'Dir',       cell: (i) => <span className={i.getValue() === 'BUY' ? 'text-green-400' : 'text-red-400'}>{i.getValue()}</span> }),
  col.accessor('quantity',  { header: 'Qty' }),
  col.accessor('filled',    { header: 'Filled' }),
  col.accessor('status',    { header: 'Status',    cell: (i) => <span className={STATUS_COLOR[i.getValue()] ?? ''}>{i.getValue()}</span> }),
  col.accessor('orderType', { header: 'Type' }),
  col.accessor('avgPrice',  { header: 'Avg Price', cell: (i) => `₹${i.getValue().toFixed(2)}` }),
  col.accessor('placedAt',  { header: 'Placed' }),
  col.accessor('tag',       { header: 'Tag',       cell: (i) => <span className="text-slate-500">{i.getValue()}</span> }),
]

export default function OrderMonitor() {
  const { orders } = useDashboard()
  const table = useReactTable({ data: orders, columns: COLUMNS, getCoreRowModel: getCoreRowModel() })

  return (
    <div className="bg-slate-900 rounded border border-slate-800 overflow-x-auto">
      <p className="text-xs text-slate-500 px-4 pt-4 pb-2">Orders ({orders.length})</p>
      <table className="w-full text-xs">
        <thead>
          {table.getHeaderGroups().map((hg) => (
            <tr key={hg.id} className="border-b border-slate-800 text-slate-500">
              {hg.headers.map((h) => (
                <th key={h.id} className="text-left px-4 py-2 font-normal">
                  {flexRender(h.column.columnDef.header, h.getContext())}
                </th>
              ))}
            </tr>
          ))}
        </thead>
        <tbody>
          {table.getRowModel().rows.length === 0 ? (
            <tr>
              <td colSpan={COLUMNS.length} className="px-4 py-6 text-slate-600 text-center">No orders today</td>
            </tr>
          ) : (
            table.getRowModel().rows.map((row) => (
              <tr key={row.id} className="border-b border-slate-800/50 hover:bg-slate-800/30">
                {row.getVisibleCells().map((cell) => (
                  <td key={cell.id} className="px-4 py-2 text-slate-300">
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  )
}
