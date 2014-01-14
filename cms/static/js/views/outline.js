define(["jquery", "underscore", "js/views/baseview"], function($, _, BaseView) {
    var Outline = BaseView.extend({
        events : {
        },

        initialize: function() {
            this.template = _.template($("#outline-tpl").text());
        },

        render: function() {
            this.$el.html(this.template());
            return this;
        }
    });

    return Outline;
});
