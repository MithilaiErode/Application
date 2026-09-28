// SYNTHETIC SAMPLE - Part Maintenance customisation (script only)
using System;
using System.Windows.Forms;
using Ice.Lib.Customization;
using Ice.Lib.Framework;
using AcmePricing;   // third-party pricing DLL installed on each client PC

public class Script
{
    private EpiDataView edvPart;
    private Button btnCalcPrice;

    public void InitializeCustomCode()
    {
        edvPart = (EpiDataView)oTrans.EpiDataViews["Part"];
        btnCalcPrice = new Button { Text = "Calc List Price", Left = 620, Top = 40, Width = 120 };
        btnCalcPrice.Click += BtnCalcPrice_Click;
        ((Control)csm.GetNativeControlReference("5f3e2a1b-detail-panel")).Controls.Add(btnCalcPrice);
    }

    public void DestroyCustomCode()
    {
        btnCalcPrice.Click -= BtnCalcPrice_Click;
    }

    private void BtnCalcPrice_Click(object sender, EventArgs e)
    {
        if (edvPart.Row < 0) return;
        decimal cost = Convert.ToDecimal(edvPart.dataView[edvPart.Row]["UnitPrice"]);
        string cls = edvPart.dataView[edvPart.Row]["ClassID"].ToString();
        var engine = new PriceEngine(@"\fileserver\pricing\rules.xml");
        decimal list = engine.CalculateListPrice(cost, cls);
        edvPart.dataView[edvPart.Row]["UnitPrice"] = list;
        oTrans.Update();
        MessageBox.Show("List price updated to " + list.ToString("C"));
    }
}
