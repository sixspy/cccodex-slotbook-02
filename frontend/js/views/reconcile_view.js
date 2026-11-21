export const reconcileViewTemplate = `
  <div v-show="tab===4" class="panel">
    <h2>对账互证</h2>
    <p :class="recon.all_ok ? 'ok' : 'err'">{{ recon.all_ok ? '全部同源' : '存在分裂' }}</p>
    <table>
      <thead><tr><th>档</th><th>盘面已订</th><th>正式册席</th><th>占位中席</th><th>余席</th><th>结果</th></tr></thead>
      <tbody>
        <tr v-for="i in (recon.items||[])" :key="i.slot_code">
          <td>{{ i.slot_code }}</td><td>{{ i.slot_booked }}</td>
          <td>{{ i.booking_seats }}</td><td>{{ i.open_hold_seats }}</td>
          <td>{{ i.free_seats }}</td>
          <td :class="i.ok?'ok':'err'">{{ i.ok ? '通过' : '分裂' }}</td>
        </tr>
      </tbody>
    </table>
    <h3>转移审计同态</h3>
    <table>
      <thead><tr><th>任务</th><th>正式册</th><th>目标档</th><th>结果</th></tr></thead>
      <tbody>
        <tr v-for="t in (recon.transfers||[])" :key="t.job_id">
          <td>{{ t.job_id }}</td><td>{{ t.booking_id }}</td><td>{{ t.to_slot_code }}</td>
          <td :class="t.ok?'ok':'err'">{{ t.ok ? '同源' : '缺审计' }}</td>
        </tr>
      </tbody>
    </table>
  </div>
`;
